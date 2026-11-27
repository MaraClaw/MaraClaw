import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { test } from 'node:test';
import type { TestContext } from 'node:test';
import { diagnosticSchema } from '../src/diagnostics.js';
import { notifyText, socketIdentity } from '../src/input.js';
import { workerLimits } from '../src/limits.js';
import { Runtime } from '../src/runtime.js';
import { ControlledTransport, ParentStore, child, command, jid, settle, token } from './support.js';
import type { BaileysEventMap } from 'baileys';

test('socket identity rejects a LID without an authoritative PN', () => {
  assert.equal(socketIdentity({ id: '999@lid' }), null);
});

test('socket identity uses a normalized PN with an optional LID', () => {
  assert.deepEqual(socketIdentity({ id: '15550001:2@s.whatsapp.net' }),
    { type: 'identity', account_id: '15550001@s.whatsapp.net', pn_jid: '15550001@s.whatsapp.net', lid_jid: null });
  assert.deepEqual(socketIdentity({ id: '999@lid', phoneNumber: '15550001@s.whatsapp.net' }),
    { type: 'identity', account_id: '15550001@s.whatsapp.net', pn_jid: '15550001@s.whatsapp.net', lid_jid: '999@lid' });
});

test('only live self-chat messages are emitted, including fromMe false and non-BMP text', () => {
  const socket = new ControlledTransport();
  const identity = socketIdentity(socket.user);
  const message = { key: { id: 'one', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
    messageTimestamp: 123, message: { conversation: '😀'.repeat(4096) } };
  const event: BaileysEventMap['messages.upsert'] = { type: 'notify', messages: [message] };
  assert.equal(notifyText(event, identity)[0]?.type, 'message');
  assert.deepEqual(notifyText({ ...event, type: 'append' }, identity), []);
  assert.deepEqual(notifyText({ ...event, type: 'append', requestId: 'history' }, identity), []);
  // The SDK emits a phone-recovered live text as notify with a request ID (see sdk-recovery.test.ts).
  assert.equal(notifyText({ ...event, requestId: 'recovered' }, identity)[0]?.type, 'message');
  assert.deepEqual(notifyText({ ...event, messages: [{ ...message, key: { ...message.key, remoteJid: '123@s.whatsapp.net' } }] }, identity), []);
  assert.deepEqual(notifyText({ ...event, messages: [{ ...message, message: { conversation: '😀'.repeat(4097) } }] }, identity), []);
});

test('failed credential commit closes runtime before transport opens', async () => {
  const store = new ParentStore();
  store.accept = false;
  let opened = 0;
  let exited = 0;
  const runtime = new Runtime({ factory: () => { opened++; return new ControlledTransport(); },
    write: line => store.write(line), exit: () => { exited++; } });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 });
  await settle();
  assert.equal(opened, 0);
  assert.equal(exited, 1);
  assert.equal(store.businessFrames.at(-1)?.['reason'], 'auth_store_failed');
  store.peer.close();
});

test('lease watchdog ends the active transport and exits', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const store = new ParentStore();
  const socket = new ControlledTransport();
  let exited = 0;
  const runtime = new Runtime({ factory: () => socket, write: line => store.write(line), exit: () => { exited++; } });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 1000 });
  await settle();
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(socket.ended, 1);
  assert.equal(exited, 1);
  assert.equal(store.businessFrames.at(-1)?.['reason'], 'lease_expired');
  store.peer.close();
});

const HEALTHY_MS = 60_000;

function closeWith(socket: ControlledTransport, code: number): void {
  socket.ev.emit('connection.update', { connection: 'close',
    lastDisconnect: { error: Object.assign(new Error('closed'), { output: { statusCode: code } }), date: new Date() } });
}

async function started(t: TestContext, jitter?: (ms: number) => number) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const store = new ParentStore();
  const sockets: ControlledTransport[] = [];
  let exited = 0;
  const runtime = new Runtime({ factory: () => { const socket = new ControlledTransport(); sockets.push(socket); return socket; },
    write: line => store.write(line), exit: () => { exited++; }, ...(jitter === undefined ? {} : { jitter }) });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 120_000 });
  await settle();
  const open = async (): Promise<ControlledTransport> => {
    const socket = sockets.at(-1);
    assert.ok(socket);
    runtime.receive({ type: 'lease', id: 'lease', token, lease_ms: 120_000 });
    socket.ev.emit('connection.update', { connection: 'open' });
    await settle();
    return socket;
  };
  const reconnectDelays = (): unknown[] => store.frames
    .filter(frame => frame['stage'] === 'reconnect' && frame['event'] === 'start').map(frame => frame['delay_ms']);
  return { store, sockets, runtime, open, reconnectDelays, exited: () => exited };
}

test('a connection that stays open for the healthy interval resets the reconnect budget', async t => {
  const { store, sockets, open, reconnectDelays } = await started(t, ms => ms);
  for (const expected of [1000, 2000]) {
    const socket = await open();
    closeWith(socket, 500);
    await settle();
    assert.equal(reconnectDelays().at(-1), expected);
    t.mock.timers.tick(expected);
    await settle();
  }
  // Given a third connection that stays open past the healthy interval.
  const healthy = await open();
  t.mock.timers.tick(HEALTHY_MS);
  closeWith(healthy, 500);
  await settle();
  // Then the next delay starts from the first step again.
  assert.equal(reconnectDelays().at(-1), 1000);
  assert.equal(sockets.length, 3);
  store.peer.close();
});

test('a flapping socket never earns a reset and ends with transport_failed', async t => {
  const { store, sockets, open, exited, reconnectDelays } = await started(t, ms => ms);
  for (const delay of [1000, 2000, 4000, 8000, 16000]) {
    const socket = await open();
    t.mock.timers.tick(HEALTHY_MS - 1);
    closeWith(socket, 500);
    await settle();
    assert.equal(reconnectDelays().at(-1), delay);
    t.mock.timers.tick(delay);
    await settle();
  }
  closeWith(await open(), 500);
  await settle();
  assert.equal(sockets.length, 6);
  assert.equal(exited(), 1);
  assert.equal(store.businessFrames.at(-1)?.['reason'], 'transport_failed');
  store.peer.close();
});

test('reconnect delays go through the injected jitter and arm exactly one timer', async t => {
  const { store, sockets, open, reconnectDelays } = await started(t, ms => ms / 2);
  closeWith(await open(), 500);
  await settle();
  assert.deepEqual(reconnectDelays(), [500]);
  t.mock.timers.tick(499);
  await settle();
  assert.equal(sockets.length, 1);
  t.mock.timers.tick(1);
  await settle();
  assert.equal(sockets.length, 2);
  t.mock.timers.tick(60_000);
  await settle();
  assert.equal(sockets.length, 2);
  store.peer.close();
});

for (const [code, reason] of [[401, 'logged_out'], [440, 'replaced'], [403, 'forbidden']] as const) {
  test(`close code ${code} stays terminal as ${reason} and never reconnects`, async t => {
    const { store, sockets, open, exited } = await started(t, ms => ms);
    closeWith(await open(), code);
    await settle();
    t.mock.timers.tick(120_000);
    await settle();
    assert.equal(sockets.length, 1);
    assert.equal(exited(), 1);
    assert.equal(store.businessFrames.at(-1)?.['reason'], reason);
    store.peer.close();
  });
}

test('a closing socket revokes intake and marks pending sends uncertain before the barrier settles', async t => {
  const { store, runtime, open } = await started(t, ms => ms);
  const socket = await open();
  const upsert = (id: string): void => {
    socket.ev.emit('messages.upsert', { type: 'notify', messages: [{ key: { id, remoteJid: jid, fromMe: true },
      messageTimestamp: 1, message: { conversation: id } }] });
  };
  upsert('live');
  runtime.receive(command());
  await settle();
  assert.equal(socket.sends.length, 1);
  const frames = (type: string): number => store.businessFrames.filter(frame => frame.type === type).length;
  assert.equal(frames('message'), 1);
  // Given a credential write that has not been acknowledged, so the barrier is pending.
  store.automatic = false;
  socket.ev.emit('creds.update', { platform: 'pending' });
  // When the socket closes and a message arrives before the barrier settles.
  closeWith(socket, 500);
  upsert('late');
  // Then intake is already revoked and the in-flight send is already uncertain.
  assert.equal(frames('message'), 1);
  assert.equal(store.businessFrames.filter(frame => frame.type === 'send_result').at(-1)?.['outcome'], 'uncertain');
  store.peer.close();
});

test('the parent heap ceiling becomes the worker old-generation limit and unrelated arguments are ignored', () => {
  assert.deepEqual(workerLimits([]), {});
  assert.deepEqual(workerLimits(['--worker-heap-mb=512']), { maxOldGenerationSizeMb: 512 });
  assert.deepEqual(workerLimits(['dist/src/bridge.js', '--worker-heap-mb=64', '--other']), { maxOldGenerationSizeMb: 64 });
});

for (const given of ['--worker-heap-mb=', '--worker-heap-mb=0', '--worker-heap-mb=-1', '--worker-heap-mb=1e3', '--worker-heap-mb=512MB', '--worker-heap-mb=100000']) {
  test(`a malformed heap ceiling ${given} is refused instead of widening the worker`, () => {
    assert.throws(() => workerLimits([given]), RangeError);
  });
}

test('a worker that outgrows its heap ceiling ends only its bridge with a failing status after a failure diagnostic', { timeout: 20_000 }, async t => {
  // Given the real bridge supervisor over a worker that allocates until it is out of memory under a 32 MiB ceiling.
  const bridge = spawn(process.execPath, [new URL('./oom-bridge.js', import.meta.url).pathname, '--worker-heap-mb=32'],
    { stdio: ['pipe', 'pipe', 'ignore'] });
  t.after(() => { bridge.kill('SIGKILL'); });
  let stdout = '';
  bridge.stdout.setEncoding('utf8').on('data', (chunk: string) => { stdout += chunk; });
  const closed = once(bridge, 'close');
  // When the parent initializes it and the worker runs out of memory.
  bridge.stdin.write(JSON.stringify({ type: 'init', id: 'init', token, creds: null, lease_ms: 120_000 }) + '\n');
  // Then the bridge exits non-zero on its own, which the parent reads as a transient child death, never a clean exit.
  assert.deepEqual(await closed, [1, null]);
  const frames = stdout.split('\n').filter(line => line !== '').map(line => child.parse(JSON.parse(line)));
  const failure = frames.find(frame => frame['stage'] === 'worker_exit');
  assert.ok(failure);
  assert.equal(failure.type, 'diagnostic');
  assert.equal(failure.token, token);
  assert.equal(failure['event'], 'failure');
  const { id: _id, token: _token, ...payload } = failure;
  diagnosticSchema.parse(payload);
});
