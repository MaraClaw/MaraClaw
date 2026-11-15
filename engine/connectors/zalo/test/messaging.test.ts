import assert from 'node:assert/strict';
import { test } from 'node:test';
import { GroupMessage, ThreadType, UserMessage } from 'zca-js';
import { Harness, messageData, token } from './support.js';

test('only private live text reaches intake with stable uidFrom and integer seconds', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  await h.connect();
  const listener = h.sdk.api.listener;
  listener.emit('message', new GroupMessage('10001', { ...messageData(), mentions: undefined }));
  for (const overrides of [{ uidFrom: '0' }, { uidFrom: '10001' }, { content: { title: 'image' } },
    { content: '' }, { content: ' \n' }, { msgType: 'chat.photo', content: 'caption' },
    { ts: 'NaN' }, { ts: '9007199254740992' }]) {
    listener.emit('message', new UserMessage('10001', messageData(overrides)));
  }
  listener.emit('message', new UserMessage('10001', messageData()));
  const messages = h.frames.filter(frame => frame.type === 'message');
  assert.equal(messages.length, 1);
  const received = messages[0];
  assert.ok(received);
  assert.deepEqual({ sender: received.sender_id, thread: received.thread_id, fromMe: received.from_me,
    timestamp: received.timestamp, text: received.text },
  { sender: '20002', thread: '20002', fromMe: false, timestamp: 1700000000, text: 'hello from Zalo' });
  const longText = 'x'.repeat(2001);
  listener.emit('message', new UserMessage('10001', messageData({ msgId: 'message-2', content: longText })));
  assert.equal(h.frames.filter(frame => frame.type === 'message')[1]?.text, longText);
});
for (const mode of ['ack', 'no-ack', 'throw-after-dispatch'] as const) {
  test(`send ${mode} reports provider outcome and never redispatches a replay`, async context => {
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    await h.connect();
    switch (mode) {
      case 'ack': break;
      case 'no-ack': h.sdk.api.response = async () => ({ message: null, attachment: [{ msgId: 999 }] }); break;
      case 'throw-after-dispatch': h.sdk.api.response = () => { throw new Error('secret-cookie-provider-error'); }; break;
    }
    const command = { id: 's', token, type: 'send', command_id: 'command-1', thread_id: '20002', text: 'outgoing text' } as const;
    h.runtime.receive(command);
    const result = await h.waitFor('send_result');
    assert.equal(result.outcome, mode === 'ack' ? 'accepted' : 'uncertain');
    assert.equal(result.message_id, mode === 'ack' ? '97531' : '');
    h.runtime.receive({ ...command, id: 'replay' });
    assert.deepEqual(h.sdk.api.dispatched, [{ text: 'outgoing text', threadId: '20002', type: ThreadType.User }]);
    assert.equal(h.frames.filter(frame => frame.type === 'send_result').length, 2);
    assert.equal(JSON.stringify(h.frames).includes('secret-cookie-provider-error'), false);
  });
}
test('disconnect revokes intake and resolves in-flight sends as uncertain without reconnect', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  await h.connect();
  const pending = Promise.withResolvers<{ message: { msgId: number }; attachment: [] }>();
  h.sdk.api.response = () => pending.promise;
  h.runtime.receive({ id: 's', token, type: 'send', command_id: 'inflight', thread_id: '20002', text: 'pending' });
  h.sdk.api.listener.emit('disconnected');
  assert.equal((await h.waitFor('send_result')).outcome, 'uncertain');
  assert.equal((await h.waitFor('closed')).reason, 'transport_failed');
  h.sdk.api.listener.emit('message', new UserMessage('10001', messageData()));
  pending.resolve({ message: { msgId: 100 }, attachment: [] });
  await pending.promise;
  assert.equal(h.frames.filter(frame => frame.type === 'message').length, 0);
  assert.equal(h.frames.filter(frame => frame.type === 'send_result').length, 1);
  assert.equal(h.sdk.api.listener.started, false);
  assert.equal(h.exits, 1);
});
