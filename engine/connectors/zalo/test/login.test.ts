import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LoginQRCallbackEventType, UserMessage } from 'zca-js';
import { credentials, Harness, messageData, png } from './support.js';

test('fresh QR emits PNG and only resolved API credentials are committed before connected intake', async context => {
  context.mock.timers.enable({ apis: ['Date'], now: 1700000000000 });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.init(null);
  h.sdk.generateQr();
  const qr = await h.waitFor('qr');
  assert.equal(qr.value, png);
  assert.equal(qr.expires_at, '2023-11-14T22:15:00.000Z');
  h.sdk.callback?.({ type: LoginQRCallbackEventType.GotLoginInfo, data: { ...credentials, imei: 'too-early-imei' }, actions: null });
  assert.deepEqual(h.frames.map(frame => frame.type), ['qr']);
  h.sdk.completeQr();
  const commit = await h.waitFor('creds_set');
  assert.deepEqual(JSON.parse(commit.creds), credentials);
  h.sdk.api.listener.emit('message', new UserMessage('10001', messageData()));
  assert.equal(h.sdk.api.listener.started, false);
  assert.deepEqual(h.frames.map(frame => frame.type), ['qr', 'creds_set']);
  h.ack(commit.id);
  await h.waitFor('ready');
  h.sdk.api.listener.emit('message', new UserMessage('10001', messageData()));
  assert.deepEqual(h.frames.map(frame => frame.type), ['qr', 'creds_set', 'identity', 'ready', 'message']);
  assert.equal((await h.waitFor('identity')).account_id, '10001');
});
test('restore calls cookie login, persists current context and waits for listener connection', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.sdk.api.listener.autoConnect = false;
  h.init();
  const commit = await h.waitFor('creds_set');
  assert.deepEqual(h.sdk.restored, credentials);
  assert.equal(h.sdk.qrCalls, 0);
  assert.deepEqual(h.frames.map(frame => frame.type), ['creds_set']);
  h.ack(commit.id);
  await h.waitFor('identity');
  assert.deepEqual(h.frames.map(frame => frame.type), ['creds_set', 'identity']);
  h.sdk.api.listener.emit('connected');
  assert.equal((await h.waitFor('ready')).token, commit.token);
});
for (const event of [LoginQRCallbackEventType.QRCodeExpired, LoginQRCallbackEventType.QRCodeDeclined]) {
  test(`QR event ${event} closes, clears the displayed code and aborts without retry`, async context => {
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    h.init(null);
    h.sdk.generateQr();
    const actions = { abort: () => { h.sdk.aborted++; }, retry: () => assert.fail('no retry') };
    switch (event) {
      case LoginQRCallbackEventType.QRCodeExpired: h.sdk.callback?.({ type: event, data: null, actions }); break;
      case LoginQRCallbackEventType.QRCodeDeclined: h.sdk.callback?.({ type: event, data: { code: 'declined' }, actions }); break;
    }
    assert.equal((await h.waitFor('closed')).reason, event === LoginQRCallbackEventType.QRCodeExpired ? 'qr_expired' : 'qr_declined');
    assert.equal(h.sdk.aborted, 1);
    assert.equal(h.sdk.qrCalls, 1);
    assert.equal(h.exits, 1);
  });
}
test('local QR expiry closes even when SDK expiration callback never arrives', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const h = new Harness();
  h.init(null);
  h.sdk.generateQr();
  context.mock.timers.tick(100_000);
  assert.equal((await h.waitFor('closed')).reason, 'qr_expired');
  assert.equal(h.sdk.aborted, 1);
  assert.equal(h.exits, 1);
});
for (const mode of ['restore-failed', 'malformed-credentials', 'invalid-json', 'commit-rejected', 'commit-timeout', 'wrong-request'] as const) {
  test(`${mode} fails closed without QR fallback or intake`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout'] });
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    h.sdk.restoreFailure = mode === 'restore-failed';
    h.init(mode === 'malformed-credentials' ? '{}' : mode === 'invalid-json' ? '{' : JSON.stringify(credentials));
    switch (mode) {
      case 'restore-failed': case 'malformed-credentials': case 'invalid-json': break;
      case 'commit-rejected': h.ack((await h.waitFor('creds_set')).id, false); break;
      case 'wrong-request': await h.waitFor('creds_set'); h.ack('unrelated'); break;
      case 'commit-timeout': await h.waitFor('creds_set'); context.mock.timers.tick(15_000); break;
    }
    const closed = await h.waitFor('closed');
    const reason = mode === 'wrong-request' ? 'protocol_error' : mode.startsWith('commit') ? 'auth_store_failed'
      : mode === 'restore-failed' ? 'transport_failed' : 'login_failed';
    assert.equal(closed.reason, reason);
    assert.equal(h.sdk.qrCalls, 0);
    assert.equal(h.sdk.api.listener.started, false);
    assert.equal(h.frames.some(frame => frame.type === 'identity' || frame.type === 'ready' || frame.type === 'message'), false);
});
}
