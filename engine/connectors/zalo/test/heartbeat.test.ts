import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import type { TestContext } from 'node:test';
import { UserMessage } from 'zca-js';
import { Harness, messageData, token } from './support.js';

function renew(h: Harness): void {
  h.runtime.receive({ id: 'lease', token, type: 'lease', lease_ms: 120_000 });
}
async function advance(h: Harness, context: TestContext, ms: number): Promise<void> {
  for (let left = ms; left > 0; left -= 60_000) {
    renew(h);
    context.mock.timers.tick(Math.min(60_000, left));
    await setImmediate();
  }
}
async function connect(h: Harness, interval?: number): Promise<void> {
  h.init(undefined, interval);
  h.ack((await h.waitFor('creds_set')).id);
  await h.waitFor('ready');
}
const send = { id: 'send', token, type: 'send', thread_id: '20002', text: 'hello', command_id: 'after-refresh' } as const;
const typing = { id: 'typing', token, type: 'typing', thread_id: '20002' } as const;

for (const interval of [undefined, 60_000, 1_800_000]) {
  test(`heartbeat uses operator cadence ${interval ?? 'default'} only after ready and settlement`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout'] });
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    const pending = Promise.withResolvers<unknown>();
    h.sdk.api.ping = () => pending.promise;
    await connect(h, interval);
    await advance(h, context, (interval ?? 300_000) - 1);
    assert.equal(h.sdk.api.pingCalls, 0);
    await advance(h, context, 1);
    assert.equal(h.sdk.api.pingCalls, 1);
    context.mock.timers.tick(14_999);
    assert.equal(h.frames.filter(frame => frame.type === 'heartbeat').length, 0);
    pending.resolve({});
    await setImmediate();
    assert.deepEqual(h.frames.filter(frame => frame.type === 'heartbeat').map(frame => frame.ok), [true]);
    await advance(h, context, (interval ?? 300_000) - 1);
    assert.equal(h.sdk.api.pingCalls, 1);
    await advance(h, context, 1);
    assert.equal(h.sdk.api.pingCalls, 2);
  });
}
test('heartbeat does not start before listener ready', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.sdk.api.listener.autoConnect = false;
  h.init(undefined, 60_000);
  h.ack((await h.waitFor('creds_set')).id);
  await h.waitFor('identity');
  await advance(h, context, 60_000);
  assert.equal(h.sdk.api.pingCalls, 0);
  h.sdk.api.listener.emit('connected');
  await advance(h, context, 60_000);
  assert.equal((await h.waitFor('heartbeat')).ok, true);
});
test('stuck heartbeat closes once at 15 seconds and ignores late provider settlement', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  const pending = Promise.withResolvers<unknown>();
  h.sdk.api.ping = () => pending.promise;
  await connect(h, 60_000);
  await advance(h, context, 60_000);
  await advance(h, context, 15_000);
  assert.equal((await h.waitFor('closed')).reason, 'transport_failed');
  assert.deepEqual(h.frames.filter(frame => frame.type === 'heartbeat').map(frame => frame.ok), [false]);
  pending.resolve({});
  await setImmediate();
  context.mock.timers.tick(1_800_000);
  assert.equal(h.sdk.api.pingCalls, 1);
  assert.equal(h.exits, 1);
  assert.equal(h.frames.filter(frame => frame.type === 'heartbeat').length, 1);
});
test('three consecutive settled heartbeat failures close transport and success resets the streak', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  const outcomes = [false, false, true, false, false, false];
  h.sdk.api.ping = async () => {
    if (!outcomes[h.sdk.api.pingCalls - 1]) throw new Error('private-session-invalid-localized-message');
  };
  await connect(h, 60_000);
  for (let i = 0; i < outcomes.length; i++) {
    await advance(h, context, 60_000);
    assert.equal(h.exits, i === 5 ? 1 : 0);
  }
  assert.deepEqual(h.frames.filter(frame => frame.type === 'heartbeat').map(frame => frame.ok), outcomes);
  assert.equal((await h.waitFor('closed')).reason, 'transport_failed');
  assert.equal(JSON.stringify(h.frames).includes('private-session-invalid-localized-message'), false);
});
for (const failed of [false, true]) {
  test(`settled heartbeat ${failed ? 'failure' : 'success'} commits cookie mutations serially before provider work`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout'] });
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    h.sdk.api.ping = async () => {
      h.sdk.api.cookies[0] = { key: 'session', value: 'refresh-one', domain: 'zalo.me', path: '/' };
      if (failed) throw new Error('private-provider-error');
    };
    await connect(h, 60_000);
    const offset = h.frames.length;
    await advance(h, context, 60_000);
    const first = await h.waitFor('creds_set', offset);
    assert.equal(JSON.parse(first.creds).cookie[0].value, 'refresh-one');
    h.runtime.receive({ ...send, command_id: 'blocked' });
    h.runtime.receive(typing);
    h.sdk.api.listener.emit('message', new UserMessage('10001', messageData()));
    assert.equal(h.sdk.api.dispatched.length, 0);
    assert.equal(h.sdk.api.typed.length, 0);
    assert.equal(h.frames.filter(frame => frame.type === 'message' || frame.type === 'heartbeat').length, 0);
    h.sdk.api.cookies[0] = { key: 'session', value: 'refresh-two', domain: 'zalo.me', path: '/' };
    const secondOffset = h.frames.length;
    h.ack(first.id);
    await setImmediate();
    assert.equal(h.frames.filter(frame => frame.type === 'creds_set').length, 3);
    const second = await h.waitFor('creds_set', secondOffset);
    assert.notEqual(second.id, first.id);
    assert.equal(JSON.parse(first.creds).cookie[0].value, 'refresh-one');
    assert.equal(JSON.parse(second.creds).cookie[0].value, 'refresh-two');
    h.ack(second.id);
    assert.equal((await h.waitFor('heartbeat')).ok, !failed);
    h.runtime.receive(send);
    h.runtime.receive(typing);
    h.sdk.api.listener.emit('message', new UserMessage('10001', messageData()));
    await setImmediate();
    assert.equal(h.sdk.api.dispatched.length, 1);
    assert.equal(h.sdk.api.typed.length, 1);
    assert.equal(h.frames.filter(frame => frame.type === 'message').length, 1);
    h.sdk.api.ping = async () => ({});
    await advance(h, context, 60_000);
    assert.equal(h.frames.filter(frame => frame.type === 'creds_set').length, 3);
  });
}
for (const mode of ['nack', 'timeout', 'wrong-ack'] as const) {
  test(`credential refresh ${mode} closes without another heartbeat or provider call`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout'] });
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    await connect(h, 60_000);
    h.sdk.api.ping = async () => { h.sdk.api.cookies.length = 0;
      h.sdk.api.cookies.push({ key: 'session', value: 'new', domain: 'zalo.me', path: '/' }); };
    const offset = h.frames.length;
    await advance(h, context, 60_000);
    const commit = await h.waitFor('creds_set', offset);
    if (mode === 'timeout') await advance(h, context, 15_000);
    else h.ack(mode === 'wrong-ack' ? 'unrelated' : commit.id, false);
    assert.equal((await h.waitFor('closed')).reason, mode === 'wrong-ack' ? 'protocol_error' : 'auth_store_failed');
    assert.equal(h.frames.filter(frame => frame.type === 'heartbeat').length, 0);
    h.runtime.receive(send);
    h.runtime.receive(typing);
    assert.equal(h.sdk.api.dispatched.length, 0);
    assert.equal(h.sdk.api.typed.length, 0);
    assert.equal(h.sdk.api.pingCalls, 1);
  });
}
