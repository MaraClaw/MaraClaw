import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import { ThreadType, UserMessage } from 'zca-js';
import { Harness, messageData, token } from './support.js';

const typing = { id: 'typing', token, type: 'typing', thread_id: '20002' } as const;
test('typing requires a parent command after ready and respects the four-second peer boundary', async context => {
  context.mock.timers.enable({ apis: ['Date'], now: 1700000000000 });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.init();
  h.runtime.receive(typing);
  const commit = await h.waitFor('creds_set');
  h.runtime.receive(typing);
  assert.deepEqual(h.sdk.api.typed, []);
  h.ack(commit.id);
  await h.waitFor('ready');
  h.sdk.api.listener.emit('message', new UserMessage('10001', messageData()));
  assert.deepEqual(h.sdk.api.typed, []);
  h.runtime.receive(typing);
  await setImmediate();
  h.runtime.receive(typing);
  context.mock.timers.tick(3999);
  h.runtime.receive(typing);
  assert.equal(h.sdk.api.typed.length, 1);
  context.mock.timers.tick(1);
  h.runtime.receive(typing);
  assert.deepEqual(h.sdk.api.typed, [
    { threadId: '20002', type: ThreadType.User }, { threadId: '20002', type: ThreadType.User },
  ]);
});
test('typing keeps at most four unsettled calls even past a local deadline and ignores completions after close', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1700000000000 });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  const pending = Promise.withResolvers<unknown>();
  h.sdk.api.typing = () => pending.promise;
  await h.connect();
  for (let i = 1; i <= 5; i++) h.runtime.receive({ ...typing, thread_id: String(i) });
  assert.equal(h.sdk.api.typed.length, 4);
  context.mock.timers.tick(15_000);
  h.runtime.receive({ ...typing, thread_id: '6' });
  assert.equal(h.sdk.api.typed.length, 4);
  pending.resolve({});
  await setImmediate();
  h.runtime.receive({ ...typing, thread_id: '6' });
  assert.equal(h.sdk.api.typed.length, 5);
  h.runtime.stop('shutdown');
  await setImmediate();
  h.runtime.receive({ ...typing, thread_id: '7' });
  assert.equal(h.sdk.api.typed.length, 5);
  assert.equal(h.exits, 1);
});
test('a full typing peer map cannot reset a live peer rate limit and only expired peers can be replaced', async context => {
  context.mock.timers.enable({ apis: ['Date'], now: 1700000000000 });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  await h.connect();
  for (let i = 1; i <= 1000; i++) {
    h.runtime.receive({ ...typing, thread_id: String(i) });
    await setImmediate();
  }
  h.runtime.receive({ ...typing, thread_id: '1001' });
  await setImmediate();
  h.runtime.receive({ ...typing, thread_id: '1' });
  assert.equal(h.sdk.api.typed.length, 1000);
  context.mock.timers.tick(4000);
  h.runtime.receive({ ...typing, thread_id: '1001' });
  await setImmediate();
  h.runtime.receive({ ...typing, thread_id: '1001' });
  assert.equal(h.sdk.api.typed.length, 1001);
  assert.equal(h.sdk.api.typed.at(-1)?.threadId, '1001');
});
test('synchronous and asynchronous typing failures release settled capacity without failing a send or leaking errors', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  await h.connect();
  h.sdk.api.typing = () => { throw new Error('private-sync-typing-cookie'); };
  h.runtime.receive(typing);
  await setImmediate();
  h.sdk.api.typing = async () => { throw { secret: 'private-async-typing-cookie' }; };
  h.runtime.receive({ ...typing, thread_id: '30003' });
  await setImmediate();
  h.runtime.receive({ id: 's', token, type: 'send', command_id: 'send', thread_id: '20002', text: 'outgoing' });
  assert.equal((await h.waitFor('send_result')).outcome, 'accepted');
  assert.equal(h.exits, 0);
  assert.equal(h.sdk.api.typed.length, 2);
  assert.equal(JSON.stringify(h.frames).includes('typing-cookie'), false);
});
