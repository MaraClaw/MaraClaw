import assert from 'node:assert/strict';
import { test } from 'node:test';
import { notifyText, socketIdentity } from '../src/input.js';
import { Runtime } from '../src/runtime.js';
import { ControlledTransport, ParentStore, settle, token } from './support.js';
import type { BaileysEventMap } from 'baileys';

test('socket identity rejects a LID without an authoritative PN', () => {
  assert.equal(socketIdentity({ id: '999@lid' }), null);
});

test('socket identity uses a normalized PN with an optional LID', () => {
  assert.deepEqual(socketIdentity({ id: '15550001:2@s.whatsapp.net' }),
    { type: 'identity', account_id: '15550001@s.whatsapp.net', pn_jid: '15550001@s.whatsapp.net', lid_jid: null });
  assert.deepEqual(socketIdentity({ id: '999@lid', phoneNumber: '15550001@s.whatsapp.net' }),
    { type: 'identity', account_id: '15550001@s.whatsapp.net', pn_jid: '15550001@s.whatsapp.net', lid_jid: '999@lid' });
});

test('only live self-chat messages are emitted, including fromMe false and non-BMP text', () => {
  const socket = new ControlledTransport();
  const identity = socketIdentity(socket.user);
  const message = { key: { id: 'one', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
    messageTimestamp: 123, message: { conversation: '😀'.repeat(4096) } };
  const event: BaileysEventMap['messages.upsert'] = { type: 'notify', messages: [message] };
  assert.equal(notifyText(event, identity)[0]?.type, 'message');
  assert.deepEqual(notifyText({ ...event, type: 'append' }, identity), []);
  assert.deepEqual(notifyText({ ...event, type: 'append', requestId: 'history' }, identity), []);
  // The SDK emits a phone-recovered live text as notify with a request ID (see sdk-recovery.test.ts).
  assert.equal(notifyText({ ...event, requestId: 'recovered' }, identity)[0]?.type, 'message');
  assert.deepEqual(notifyText({ ...event, messages: [{ ...message, key: { ...message.key, remoteJid: '123@s.whatsapp.net' } }] }, identity), []);
  assert.deepEqual(notifyText({ ...event, messages: [{ ...message, message: { conversation: '😀'.repeat(4097) } }] }, identity), []);
});

test('failed credential commit closes runtime before transport opens', async () => {
  const store = new ParentStore();
  store.accept = false;
  let opened = 0;
  let exited = 0;
  const runtime = new Runtime({ factory: () => { opened++; return new ControlledTransport(); },
    write: line => store.write(line), exit: () => { exited++; } });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 });
  await settle();
  assert.equal(opened, 0);
  assert.equal(exited, 1);
  assert.equal(store.businessFrames.at(-1)?.['reason'], 'auth_store_failed');
  store.peer.close();
});

test('lease watchdog ends the active transport and exits', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const store = new ParentStore();
  const socket = new ControlledTransport();
  let exited = 0;
  const runtime = new Runtime({ factory: () => socket, write: line => store.write(line), exit: () => { exited++; } });
  store.reply = frame => runtime.receive(frame);
  runtime.receive({ type: 'init', id: 'init', token, creds: null, lease_ms: 1000 });
  await settle();
  t.mock.timers.tick(1000);
  await settle();
  assert.equal(socket.ended, 1);
  assert.equal(exited, 1);
  assert.equal(store.businessFrames.at(-1)?.['reason'], 'lease_expired');
  store.peer.close();
});
