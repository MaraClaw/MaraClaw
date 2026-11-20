import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Sends } from '../src/sends.js';
import { socketIdentity } from '../src/input.js';
import type { Identity } from '../src/input.js';
import type { ChildPayload, SendCommand } from '../src/protocol.js';
import { command, ControlledTransport, deferred, jid, settle } from './support.js';

type Live = { readonly socket: ControlledTransport; readonly identity: Identity } | null;
const confirmed = socketIdentity(new ControlledTransport().user);
assert.ok(confirmed);
const outcomes = (output: ChildPayload[]): (string | null)[] => output.map(value => value.type === 'send_result' ? value.outcome : null);
function harness(initial: Live, known: Identity | null = confirmed): { readonly sends: Sends; readonly output: ChildPayload[]; readonly stops: () => number; live: Live } {
  let stopped = 0;
  const output: ChildPayload[] = [];
  const state: { sends: Sends; output: ChildPayload[]; stops: () => number; live: Live } = {
    sends: new Sends({ emit: value => output.push(value), barrier: async () => undefined,
      current: () => state.live, known: () => known, failed: () => { stopped++; } }),
    output, stops: () => stopped, live: initial };
  return state;
}
function connected(): { readonly socket: ControlledTransport; readonly identity: Identity } {
  const socket = new ControlledTransport();
  const identity = socketIdentity(socket.user);
  assert.ok(identity);
  return { socket, identity };
}

test('local send is not an acknowledgement and uses the reserved ID', async () => {
  const socket = new ControlledTransport();
  const identity = socketIdentity(socket.user);
  assert.ok(identity);
  const output: ChildPayload[] = [];
  const sends = new Sends({ emit: value => output.push(value), barrier: async () => undefined,
    current: () => ({ socket, identity }), known: () => identity, failed: () => assert.fail() });
  sends.submit(command());
  await settle();
  assert.deepEqual(socket.sends, [{ jid, text: 'controlled text', id: 'RESERVED-NATIVE-ID' }]);
  assert.equal(output.length, 0);
  sends.ack(socket, { id: 'RESERVED-NATIVE-ID', jid, accepted: true });
  assert.deepEqual(output.map(value => value.type === 'send_result' ? value.outcome : null), ['accepted']);
  sends.submit(command());
  assert.equal(socket.sends.length, 1);
  assert.equal(output.length, 2);
  sends.close();
});

test('timeout before auth commit is uncertain and a duplicate never sends', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const socket = new ControlledTransport();
  const identity = socketIdentity(socket.user);
  assert.ok(identity);
  const gate = deferred<void>();
  const output: ChildPayload[] = [];
  const sends = new Sends({ emit: value => output.push(value), barrier: () => gate.promise,
    current: () => ({ socket, identity }), known: () => identity, failed: () => assert.fail() });
  sends.submit(command());
  sends.submit(command());
  t.mock.timers.tick(1000);
  gate.resolve();
  await settle();
  assert.deepEqual(socket.sends, []);
  assert.deepEqual(output.map(value => value.type === 'send_result' ? value.outcome : null), ['uncertain']);
  sends.submit(command());
  assert.equal(socket.sends.length, 0);
  sends.close();
});

test('a send between sockets is journaled as rejected and keeps the bridge running', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  // Given a bridge whose socket closed and has not reopened yet.
  const run = harness(null);
  // When the parent submits a send.
  run.sends.submit(command());
  t.mock.timers.tick(command().timeout_ms);
  await settle();
  // Then it is rejected once before the wire, no timeout outcome follows and the bridge keeps running.
  assert.deepEqual(outcomes(run.output), ['rejected']);
  assert.equal(run.stops(), 0);
  run.sends.close();
  assert.deepEqual(outcomes(run.output), ['rejected']);
});

test('a replay after reconnect re-reports the stored rejection and never dispatches', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  // Given a send rejected while the bridge had no socket.
  const run = harness(null);
  run.sends.submit(command());
  // When the socket reopens and the parent replays the same command.
  const live = connected();
  run.live = live;
  run.sends.submit(command());
  t.mock.timers.tick(command().timeout_ms);
  await settle();
  // Then the stored outcome is re-reported, nothing reaches the wire and the bridge keeps running.
  assert.deepEqual(outcomes(run.output), ['rejected', 'rejected']);
  assert.deepEqual(live.socket.sends, []);
  assert.equal(run.stops(), 0);
  // And a new command is dispatched on the reopened socket.
  run.sends.submit(command({ command_id: 'command-2', message_id: 'RESERVED-NATIVE-ID-2' }));
  await settle();
  assert.deepEqual(live.socket.sends, [{ jid, text: 'controlled text', id: 'RESERVED-NATIVE-ID-2' }]);
  run.sends.close();
});

const stopping: readonly { readonly name: string; readonly socket: boolean; readonly unpaired?: true; readonly first?: SendCommand; readonly next: SendCommand }[] = [
  { name: 'a target that is not the own JID', socket: true, next: command({ self_jid: '15550000000@s.whatsapp.net' }) },
  { name: 'a target that is not the own JID while between sockets', socket: false, next: command({ self_jid: '15550000000@s.whatsapp.net' }) },
  { name: 'a send before this bridge confirmed any identity', socket: false, unpaired: true, next: command() },
  { name: 'a target that is not normalized while between sockets', socket: false, next: command({ self_jid: '15551234567:2@s.whatsapp.net' }) },
  { name: 'a conflicting replay of a stored rejection', socket: false, first: command(), next: command({ text: 'other text' }) },
  { name: 'a duplicate message_id of a stored rejection', socket: false, first: command(), next: command({ command_id: 'command-2' }) },
];
for (const scenario of stopping) {
  test(`${scenario.name} still stops the bridge`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    // Given a bridge with or without a current socket.
    const live = scenario.socket ? connected() : null;
    const run = harness(live, scenario.unpaired ? null : confirmed);
    if (scenario.first) run.sends.submit(scenario.first);
    assert.equal(run.stops(), 0);
    // When the parent submits the offending command.
    run.sends.submit(scenario.next);
    t.mock.timers.tick(scenario.next.timeout_ms);
    await settle();
    // Then the bridge is stopped and nothing reaches the wire.
    assert.equal(run.stops(), 1);
    assert.deepEqual(live?.socket.sends ?? [], []);
    run.sends.close();
  });
}

test('the 10,000-command cap still stops the bridge when rejections fill it', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  // Given 10,000 sends journaled as rejected while the bridge had no socket.
  const run = harness(null);
  for (let index = 0; index < 10_000; index++) {
    run.sends.submit(command({ command_id: `command-${index}`, message_id: `RESERVED-${index}` }));
  }
  assert.equal(run.stops(), 0);
  assert.equal(outcomes(run.output).filter(value => value === 'rejected').length, 10_000);
  // When one more distinct command arrives after the socket reopens.
  const live = connected();
  run.live = live;
  run.sends.submit(command({ command_id: 'command-overflow', message_id: 'RESERVED-OVERFLOW' }));
  await settle();
  // Then the bridge is stopped instead of journaling or dispatching it.
  assert.equal(run.stops(), 1);
  assert.deepEqual(live.socket.sends, []);
  run.sends.close();
});
