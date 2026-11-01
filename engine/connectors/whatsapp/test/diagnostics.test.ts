import assert from 'node:assert/strict';
import { test } from 'node:test';
import { proto } from 'baileys';
import { ParentAuth } from '../src/auth.js';
import { diagnosticSchema, failure } from '../src/diagnostics.js';
import { Runtime } from '../src/runtime.js';
import { ControlledTransport, ParentStore, settle, token } from './support.js';

test('parent rejection records bounded RPC failure and preserves rejection', async () => {
  const store = new ParentStore();
  store.accept = false;
  const auth = new ParentAuth(store.peer, null, () => undefined);
  try {
    await assert.rejects(auth.save());
    const events = store.frames.filter(frame => frame.type === 'diagnostic' && frame['stage'] === 'auth_rpc');
    assert.deepEqual(events.map(frame => frame['event']), ['start', 'failure']);
    assert.equal(events[1]?.['error_kind'], 'parent_rejected');
    assert.equal(events[1]?.['rpc_id'], store.businessFrames[0]?.id);
  } finally { auth.close(); store.peer.close(); }
});

test('RPC timeout is classified and rejects the pending business request', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const store = new ParentStore();
  store.automatic = false;
  const pending = store.peer.request({ type: 'auth_get', category: 'session', ids: ['key'] });
  const rejected = assert.rejects(pending);
  t.mock.timers.tick(15000);
  await rejected;
  assert.equal(store.frames.find(frame => frame['event'] === 'failure')?.['error_kind'], 'timeout');
  store.peer.close();
});

test('closing a peer rejects the pending business RPC', async () => {
  const store = new ParentStore();
  store.automatic = false;
  const pending = store.peer.request({ type: 'auth_get', category: 'session', ids: ['key'] });
  const rejected = assert.rejects(pending);
  store.peer.close();
  await rejected;
  assert.deepEqual(store.businessFrames.map(frame => frame.type), ['auth_get']);
  const closed = store.frames.find(frame => frame['error_kind'] === 'closed');
  assert.ok(closed);
  assert.equal(closed['rpc_id'], store.businessFrames[0]?.id);
});

test('SDK key decoder failure emits category and shape without private material', async () => {
  const keyId = 'canary-key-id';
  const store = new ParentStore();
  const sdkValue = proto.Message.AppStateSyncKeyData.fromObject({ keyData: Buffer.from('sensitive-sdk-key') });
  store.values.set('session', { [keyId]: JSON.parse(JSON.stringify(sdkValue)) });
  let failures = 0;
  const auth = new ParentAuth(store.peer, null, () => { failures++; });
  try {
    await assert.rejects(Promise.resolve().then(() => auth.state.keys.get('session', [keyId])));
    const frame = store.frames.find(entry => entry['stage'] === 'auth_key_decode');
    assert.ok(frame);
    assert.equal(frame['category'], 'session');
    assert.equal(frame['value_shape'], 'object');
    assert.equal(frame['error_kind'], 'validation');
    assert.equal(failures, 1);
    const { id: _id, token: _token, ...payload } = frame;
    diagnosticSchema.parse(payload);
    assert.doesNotMatch(JSON.stringify(payload), /canary-key-id|sensitive-sdk-key/);
  } finally { auth.close(); store.peer.close(); }
});

test('diagnostic payloads omit QR, credentials, JIDs, token and error details', async () => {
  const store = new ParentStore();
  const authStore = new ParentStore();
  const auth = new ParentAuth(authStore.peer, null, () => assert.fail('store failed'));
  await auth.save({ platform: 'canary-creds' });
  const socket = new ControlledTransport();
  const runtime = new Runtime({ factory: () => socket, write: line => store.write(line), exit: () => undefined });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 });
  await settle();
  socket.ev.emit('connection.update', { connection: 'connecting', qr: 'canary-qr-secret' });
  socket.ev.emit('connection.update', { connection: 'open' });
  await settle();
  await runtime.stop('shutdown');
  const payloads = [...store.frames, ...authStore.frames].filter(frame => frame.type === 'diagnostic').map(frame => {
    const { id: _id, token: _token, ...payload } = frame;
    return diagnosticSchema.parse(payload);
  });
  assert.ok(payloads.some(frame => frame.stage === 'connection' && frame.qr_available));
  assert.ok(payloads.some(frame => frame.stage === 'identity' && frame.identity_accepted));
  const rendered = JSON.stringify([payloads, failure(new Error('canary-error /canary/path canary-creds'))]);
  for (const secret of ['canary-qr-secret', 'canary-creds', '15551234567', '123456789', token, 'canary-error', '/canary/path']) {
    assert.equal(rendered.includes(secret), false);
  }
  auth.close();
  authStore.peer.close();
  store.peer.close();
});

test('a failing diagnostic output cannot change rejection or shutdown', async () => {
  const store = new ParentStore();
  store.accept = false;
  let exited = 0;
  let opened = 0;
  const runtime = new Runtime({ factory: () => { opened++; return new ControlledTransport(); },
    write: line => {
      if (JSON.parse(line).type === 'diagnostic') throw new Error('observer unavailable');
      store.write(line);
    }, exit: () => { exited++; } });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 });
  await settle();
  assert.equal(opened, 0);
  assert.equal(exited, 1);
  assert.deepEqual(store.businessFrames.map(frame => frame.type), ['creds_set', 'closed']);
  assert.equal(store.businessFrames[1]?.['reason'], 'auth_store_failed');
  store.peer.close();
});
