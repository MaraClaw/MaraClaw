import assert from 'node:assert/strict';
import { test } from 'node:test';
import { workerLimits } from '../src/limits.js';
import { credentials, Harness, launch, token } from './support.js';

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
for (const event of ['disconnected', 'closed'] as const) {
  for (const code of [3000, 3003, 1000, 1006, 4999]) {
    test(`${event} code ${code} classifies once without provider text or reconnect`, async context => {
      const h = new Harness();
      context.after(() => h.runtime.stop('shutdown'));
      await h.connect();
      h.sdk.api.listener.emit(event, code, 'private-expired-invalid-credential-localized-reason');
      h.sdk.api.listener.emit('closed', code, 'second-close');
      assert.equal((await h.waitFor('closed')).reason, code === 3000 || code === 3003 ? 'session_replaced' : 'transport_failed');
      assert.equal(h.frames.filter(frame => frame.type === 'closed').length, 1);
      assert.equal(h.exits, 1);
      assert.equal(h.sdk.qrCalls, 0);
      assert.equal(JSON.stringify(h.frames).includes('private-expired-invalid'), false);
    });
  }
}
test('listener processor errors are handled as transport recovery without an auth inference or raw event', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  await h.connect();
  h.sdk.api.listener.emit('error', { code: 3003, cookie: 'private-error-cookie', message: 'Invalid session' });
  assert.equal((await h.waitFor('closed')).reason, 'transport_failed');
  assert.equal(h.exits, 1);
  assert.equal(JSON.stringify(h.frames).includes('private-error-cookie'), false);
});
for (const restored of [false, true]) {
  test(`${restored ? 'restore' : 'fresh QR'} login deadline preserves its failure classification`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout'] });
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    const pending = Promise.withResolvers<typeof h.sdk.api>();
    h.sdk.restoreResponse = () => pending.promise;
    h.init(restored ? JSON.stringify(credentials) : null);
    context.mock.timers.tick(60_000);
    h.runtime.receive({ id: 'lease', token, type: 'lease', lease_ms: 120_000 });
    context.mock.timers.tick(60_000);
    assert.equal((await h.waitFor('closed')).reason, restored ? 'transport_failed' : 'login_failed');
    pending.resolve(h.sdk.api);
    if (!restored) h.sdk.completeQr();
    await pending.promise;
    assert.equal(h.frames.filter(frame => frame.type === 'creds_set' || frame.type === 'ready').length, 0);
  });
}

test('the parent heap ceiling becomes the worker old-generation limit and unrelated arguments are ignored', () => {
  assert.deepEqual(workerLimits([]), {});
  assert.deepEqual(workerLimits(['--worker-heap-mb=512']), { maxOldGenerationSizeMb: 512 });
  assert.deepEqual(workerLimits(['dist/src/bridge.js', 'oom', '--worker-heap-mb=64']), { maxOldGenerationSizeMb: 64 });
});
for (const given of ['--worker-heap-mb=', '--worker-heap-mb=0', '--worker-heap-mb=-1', '--worker-heap-mb=1e3', '--worker-heap-mb=512MB', '--worker-heap-mb=100000']) {
  test(`a malformed heap ceiling ${given} is refused instead of widening the worker`, () => {
    assert.throws(() => workerLimits([given]), RangeError);
  });
}
test('a worker that outgrows its heap ceiling ends only its bridge as a transport failure', { timeout: 20_000 }, async context => {
  // Given the real supervisor over a worker that allocates past a 32 MiB ceiling once it is initialized.
  const p = launch(context, 'oom', '--worker-heap-mb=32');
  p.write({ id: 'init', token, type: 'init', lease_ms: 120_000, credentials: JSON.stringify(credentials) });
  // When the worker runs out of memory.
  const closed = await p.next();
  // Then the bridge reports the ordinary transient close and exits cleanly, so the parent retries on its durable budget.
  assert.equal(closed.type, 'closed');
  if (closed.type === 'closed') assert.equal(closed.reason, 'transport_failed');
  assert.equal(closed.token, token);
  assert.deepEqual(await p.exit, [0, null]);
});
