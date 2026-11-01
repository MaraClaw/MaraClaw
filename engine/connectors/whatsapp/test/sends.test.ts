import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Sends } from '../src/sends.js';
import { socketIdentity } from '../src/input.js';
import type { ChildPayload } from '../src/protocol.js';
import { command, ControlledTransport, deferred, jid, settle } from './support.js';

test('local send is not an acknowledgement and uses the reserved ID', async () => {
  const socket = new ControlledTransport();
  const identity = socketIdentity(socket.user);
  assert.ok(identity);
  const output: ChildPayload[] = [];
  const sends = new Sends({ emit: value => output.push(value), barrier: async () => undefined,
    current: () => ({ socket, identity }), failed: () => assert.fail() });
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
    current: () => ({ socket, identity }), failed: () => assert.fail() });
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
