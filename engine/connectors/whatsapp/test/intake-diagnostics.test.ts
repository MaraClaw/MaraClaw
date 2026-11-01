import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { WAMessage } from 'baileys';
import { diagnosticSchema } from '../src/diagnostics.js';
import type { DropReason } from '../src/diagnostics.js';
import { notifyText, socketIdentity } from '../src/input.js';
import { Runtime } from '../src/runtime.js';
import { selfMessage, sdkHarness } from './sdk-recovery.js';
import type { Upsert } from './sdk-recovery.js';
import { ControlledTransport, ParentStore, settle, token } from './support.js';

const identity = socketIdentity({ id: '15551234567:2@s.whatsapp.net', lid: '123456789:2@lid' });
const text = { conversation: 'canary-body-text' };
async function recovered(message: WAMessage): Promise<Upsert> {
  const sdk = sdkHarness();
  await sdk.recover(message, 'canary-request-id');
  const event = sdk.seen[0];
  assert.ok(event);
  return event;
}
async function started(write: (line: string) => void) {
  const store = new ParentStore();
  const socket = new ControlledTransport();
  const runtime = new Runtime({ factory: () => socket, write: line => { write(line); store.write(line); }, exit: () => undefined });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 });
  await settle();
  socket.ev.emit('connection.update', { connection: 'open' });
  await settle();
  return { store, socket, runtime };
}

const cases: readonly (readonly [DropReason, WAMessage])[] = [
  ['non_self', selfMessage('X', text, { key: { remoteJid: '15559990000@s.whatsapp.net', fromMe: true, id: 'X' } })],
  ['missing_content', { ...selfMessage('X', text), message: null }],
  ['stub', selfMessage('X', text, { messageStubType: 1 })],
  ['content_shape', selfMessage('X', { conversation: 'x', senderKeyDistributionMessage: {} })],
  ['text', selfMessage('X', { imageMessage: { caption: 'x' } })],
  ['quoted_or_forwarded', selfMessage('X', { extendedTextMessage: { text: 'x', contextInfo: { isForwarded: true } } })],
  ['timestamp', selfMessage('X', text, { messageTimestamp: -1 })],
];

for (const [reason, message] of cases) {
  test(`dropping a recovered message for ${reason} reports exactly that fixed reason`, async () => {
    const seen: DropReason[] = [];
    assert.deepEqual(notifyText(await recovered(message), identity, value => { seen.push(value); }), []);
    assert.deepEqual(seen, [reason]);
  });
}

test('event-level rejections report their own fixed reasons', async () => {
  const live = await recovered(selfMessage('X', text));
  const seen: DropReason[] = [];
  notifyText({ ...live, type: 'append' }, identity, value => { seen.push(value); });
  notifyText(live, null, value => { seen.push(value); });
  assert.deepEqual(seen, ['not_notify', 'identity_unavailable']);
});

test('the intake diagnostic carries bounded drop metadata and no message material', async () => {
  const { store, socket, runtime } = await started(() => undefined);
  socket.ev.emit('messages.upsert', await recovered(selfMessage('canary-native-id', { extendedTextMessage: { text: 'x', contextInfo: { isForwarded: true } } })));
  socket.ev.emit('messages.upsert', await recovered(selfMessage('canary-native-id-2', text)));
  await runtime.stop('shutdown');
  const intake = store.frames.filter(frame => frame['stage'] === 'intake').map(({ id: _id, token: _token, ...payload }) => diagnosticSchema.parse(payload));
  assert.deepEqual(intake.map(frame => [frame.count, frame.dropped, frame.event_kind, frame.has_request_id, frame.drop_reasons]),
    [[0, 1, 'notify', true, ['quoted_or_forwarded']], [1, 0, 'notify', true, undefined]]);
  const rendered = JSON.stringify(intake);
  for (const secret of ['canary-request-id', 'canary-native-id', 'canary-body-text', '15551234567', '123456789', token]) {
    assert.equal(rendered.includes(secret), false);
  }
  store.peer.close();
});

test('the diagnostic schema accepts only the fixed reason enum and at most nine entries', () => {
  const base = { type: 'diagnostic', stage: 'intake', event: 'finished' };
  const reasons = ['not_notify', 'identity_unavailable', 'non_self', 'missing_content', 'stub', 'content_shape', 'text', 'quoted_or_forwarded', 'timestamp'];
  assert.ok(diagnosticSchema.safeParse({ ...base, drop_reasons: reasons, event_kind: 'append', has_request_id: false }).success);
  assert.equal(diagnosticSchema.safeParse({ ...base, drop_reasons: [...reasons, 'text'] }).success, false);
  assert.equal(diagnosticSchema.safeParse({ ...base, drop_reasons: ['15551234567@s.whatsapp.net'] }).success, false);
  assert.equal(diagnosticSchema.safeParse({ ...base, event_kind: 'history' }).success, false);
  assert.equal(diagnosticSchema.safeParse({ ...base, has_request_id: 'canary-request-id' }).success, false);
});

test('a failing diagnostic output does not change message forwarding', async () => {
  const { store, socket, runtime } = await started(line => {
    if (JSON.parse(line).type === 'diagnostic') throw new Error('observer unavailable');
  });
  socket.ev.emit('messages.upsert', await recovered(selfMessage('SECOND', text)));
  await runtime.stop('shutdown');
  assert.deepEqual(store.businessFrames.filter(frame => frame.type === 'message').map(frame => frame['message_id']), ['SECOND']);
  store.peer.close();
});
