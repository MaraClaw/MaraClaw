import assert from 'node:assert/strict';
import { test } from 'node:test';
import { credentials, Harness, token } from './support.js';

test('lease renewal replaces the deadline and expires without reconnect', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.runtime.receive({ type: 'init', id: 'i', token, credentials: JSON.stringify(credentials), lease_ms: 1000 });
  await h.waitFor('creds_set');
  context.mock.timers.tick(500);
  h.runtime.receive({ type: 'lease', id: 'renewal', token, lease_ms: 2000 });
  context.mock.timers.tick(500);
  assert.equal(h.exits, 0);
  context.mock.timers.tick(1500);
  assert.equal((await h.waitFor('closed')).reason, 'lease_expired');
  assert.equal(h.exits, 1);
});
test('first non-init command stops before creating an SDK client', () => {
  const h = new Harness();
  h.runtime.receive({ type: 'lease', id: 'wrong-first', token, lease_ms: 1000 });
  assert.equal(h.exits, 1);
  assert.equal(h.sdk.restored, undefined);
  assert.equal(h.sdk.qrCalls, 0);
  assert.deepEqual(h.frames, []);
});
test('shutdown during delayed login cannot emit credentials or start a late session', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.sdk.settleAbort = false;
  h.init(null);
  h.sdk.generateQr();
  h.runtime.stop('shutdown');
  h.sdk.completeQr();
  await h.sdk.qr.promise;
  assert.deepEqual(h.frames.map(frame => frame.type), ['qr', 'closed']);
  assert.equal(h.sdk.api.listener.started, false);
  assert.equal(h.exits, 1);
});
