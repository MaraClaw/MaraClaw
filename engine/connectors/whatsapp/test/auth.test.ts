import assert from 'node:assert/strict';
import { test } from 'node:test';
import { addTransactionCapability, Curve, generateSignalPubKey, initAuthCreds, proto } from 'baileys';
import { makeLibSignalRepository } from 'baileys/lib/Signal/libsignal.js';
import pino from 'pino';
import { z } from 'zod';
import { ParentAuth } from '../src/auth.js';
import { decodeCreds, encode, parseKey } from '../src/auth-codec.js';
import { ParentStore, settle } from './support.js';

function signalRepository(auth: ParentAuth) {
  const logger = pino({ level: 'silent' });
  const keys = addTransactionCapability(auth.state.keys, logger, { maxCommitRetries: 1, delayBetweenTriesMs: 0 });
  return makeLibSignalRepository({ creds: auth.state.creds, keys }, logger);
}

test('round-trips binary keys and applies null deletions through committed parent RPC', async () => {
  const store = new ParentStore();
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  const key = Buffer.from([0, 255, 17, 42]);
  try {
    await auth.state.keys.set({ 'identity-key': { a: key }, 'pre-key': { b: { public: key, private: key } } });
    assert.deepEqual(await auth.state.keys.get('identity-key', ['a', 'missing']), { a: key });
    assert.deepEqual(await auth.state.keys.get('pre-key', ['b']), { b: { public: key, private: key } });
    await auth.state.keys.set({ 'identity-key': { a: null } });
    assert.deepEqual(await auth.state.keys.get('identity-key', ['a']), {});
  } finally { auth.close(); store.peer.close(); }
});
test('persists arbitrary Signal categories without a category whitelist', async () => {
  const store = new ParentStore();
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  try {
    const update = Object.assign({ session: {} }, { 'future-signal-category': { first: Buffer.from([4, 9]), deleted: null } });
    await auth.state.keys.set(update);
    assert.deepEqual(store.values.get('future-signal-category'), { first: { type: 'Buffer', data: 'BAk=' } });
  } finally { auth.close(); store.peer.close(); }
});
test('commits a whole multi-category SignalDataSet through one auth_set RPC', async () => {
  const store = new ParentStore();
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  try {
    const update = Object.assign({ 'identity-key': { a: Buffer.from([1]) }, 'pre-key': { b: null } },
      { 'future-signal-category': { c: '123@lid' } });
    await auth.state.keys.set(update);
    assert.deepEqual(store.frames.map(entry => entry.type), ['auth_set']);
    assert.deepEqual(store.frames[0]?.['batches'], [
      { category: 'identity-key', values: { a: { type: 'Buffer', data: 'AQ==' } } },
      { category: 'pre-key', values: { b: null } },
      { category: 'future-signal-category', values: { c: '123@lid' } },
    ]);
  } finally { auth.close(); store.peer.close(); }
});
test('a failed whole-set commit poisons the store and starts no dependent I/O', async () => {
  const store = new ParentStore();
  store.accept = false;
  let failures = 0;
  const auth = new ParentAuth(store.peer, null, () => { failures++; });
  try {
    await assert.rejects(Promise.resolve().then(() => auth.state.keys.set({ session: { a: null }, 'lid-mapping': { b: '1@lid' } })));
    await assert.rejects(Promise.resolve().then(() => auth.state.keys.get('session', ['a'])));
    assert.equal(failures, 1);
    assert.deepEqual(store.frames.map(entry => entry.type), ['auth_set']);
    assert.equal(store.values.size, 0);
  } finally { auth.close(); store.peer.close(); }
});
test('saves full merged credentials and restores BufferJSON binary material', async () => {
  const store = new ParentStore();
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  try {
    const initial = encode(auth.state.creds);
    await auth.save({ registered: true });
    await auth.save({ platform: 'controlled-platform' });
    const last = store.frames.at(-1);
    assert.ok(last);
    const restored = decodeCreds(z.string().parse(last['creds']));
    assert.equal(restored.registered, true);
    assert.equal(restored.platform, 'controlled-platform');
    assert.deepEqual(restored.noiseKey, decodeCreds(initial).noiseKey);
    assert.ok(Buffer.isBuffer(restored.noiseKey.private));
  } finally { auth.close(); store.peer.close(); }
});
test('serializes mutation RPCs and holds dependent reads until committed ACK', async () => {
  const store = new ParentStore();
  store.automatic = false;
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  try {
    const first = auth.save({ registered: true });
    const second = auth.save({ platform: 'next' });
    const read = auth.state.keys.get('lid-mapping', ['self']);
    await settle();
    assert.equal(store.frames.length, 1);
    const frame = store.frames[0]; assert.ok(frame);
    store.automatic = true;
    store.ack(frame);
    await Promise.all([first, second, read]);
    assert.deepEqual(store.frames.map(entry => entry.type), ['creds_set', 'creds_set', 'auth_get']);
  } finally { auth.close(); store.peer.close(); }
});
test('poisons the auth barrier and stops dependent work when a commit fails', async () => {
  const store = new ParentStore();
  store.accept = false;
  let failures = 0;
  const auth = new ParentAuth(store.peer, null, () => { failures++; });
  try {
    await assert.rejects(Promise.resolve().then(() => auth.state.keys.set({ 'lid-mapping': { a: '123@lid' } })));
    await assert.rejects(auth.barrier());
    await assert.rejects(Promise.resolve().then(() => auth.state.keys.get('session', ['a'])));
    assert.equal(failures, 1);
    assert.equal(store.frames.length, 1);
  } finally { auth.close(); store.peer.close(); }
});
test('fails closed when parent key material has an invalid shape', async () => {
  const store = new ParentStore();
  store.values.set('session', { invalid: 'not-binary' });
  let failures = 0;
  const auth = new ParentAuth(store.peer, null, () => { failures++; });
  try {
    await assert.rejects(Promise.resolve().then(() => auth.state.keys.get('session', ['invalid'])));
    assert.equal(failures, 1);
    await assert.rejects(auth.barrier());
  } finally { auth.close(); store.peer.close(); }
});

test('restores SDK protobuf account bytes after a committed paired-credentials save', async () => {
  const store = new ParentStore();
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  const details = Buffer.from([1, 2, 3, 4]);
  const account = proto.ADVSignedDeviceIdentity.fromObject({ details,
    accountSignatureKey: Buffer.alloc(32, 2), accountSignature: Buffer.alloc(64, 3),
    deviceSignature: Buffer.alloc(64, 4) });
  try {
    await auth.save({ account });
    const frame = store.frames.at(-1); assert.ok(frame);
    const restored = decodeCreds(z.string().parse(frame['creds']));
    assert.deepEqual(restored.account?.details, details);
    assert.deepEqual(restored.account?.accountSignatureKey, account.accountSignatureKey);
    assert.deepEqual(restored.noiseKey, auth.state.creds.noiseKey);
  } finally { auth.close(); store.peer.close(); }
});

test('reads SDK protobuf app-state key bytes after an acknowledged parent-store write', async () => {
  const store = new ParentStore();
  const auth = new ParentAuth(store.peer, null, () => assert.fail('store failed'));
  const keyData = Buffer.alloc(32, 5);
  const key = proto.Message.AppStateSyncKeyData.fromObject({ keyData,
    fingerprint: { rawId: 1, currentIndex: 0, deviceIndexes: [1, 2] }, timestamp: '123' });
  try {
    await auth.state.keys.set({ 'app-state-sync-key': { paired: key } });
    const restored = await auth.state.keys.get('app-state-sync-key', ['paired']);
    assert.deepEqual(restored['paired']?.keyData, keyData);
    assert.deepEqual(restored['paired']?.fingerprint?.deviceIndexes, [1, 2]);
    assert.equal(restored['paired']?.timestamp?.toString(), '123');
  } finally { auth.close(); store.peer.close(); }
});

test('keeps nullable protobuf fields and unknown credential fields while rejecting malformed byte strings', () => {
  const encoded = encode({ ...initAuthCreds(), providerField: 'retained',
    account: { details: null, accountSignatureKey: '', providerField: 'retained-account' } });
  const restored = decodeCreds(encoded);
  assert.equal(restored.account?.details, null);
  assert.deepEqual(restored.account?.accountSignatureKey, Buffer.alloc(0));
  const extras = z.object({ providerField: z.string(), account: z.object({ providerField: z.string() }) }).parse(restored);
  assert.equal(extras.providerField, 'retained');
  assert.equal(extras.account.providerField, 'retained-account');
  assert.throws(() => decodeCreds(encode({ ...initAuthCreds(), account: { details: '***' } })), z.ZodError);
  assert.throws(() => parseKey('app-state-sync-key', { keyData: '***' }), z.ZodError);
});

test('restores non-empty SDK sessions and exchanges messages after parent-store reload', async () => {
  const senderStore = new ParentStore();
  const receiverStore = new ParentStore();
  let failures = 0;
  const sender = new ParentAuth(senderStore.peer, null, () => { failures++; });
  const receiver = new ParentAuth(receiverStore.peer, null, () => { failures++; });
  const senderRepo = signalRepository(sender);
  const receiverRepo = signalRepository(receiver);
  const preKey = Curve.generateKeyPair();
  const receiverCreds = receiver.state.creds;
  const receiverJid = '10000000002@s.whatsapp.net';
  const senderJid = '10000000001@s.whatsapp.net';
  let restored: ParentAuth | undefined;
  let restoredRepo: ReturnType<typeof signalRepository> | undefined;
  try {
    await receiver.state.keys.set({ 'pre-key': { '1': preKey } });
    await senderRepo.injectE2ESession({ jid: receiverJid, session: {
      registrationId: receiverCreds.registrationId,
      identityKey: generateSignalPubKey(receiverCreds.signedIdentityKey.public),
      signedPreKey: { keyId: receiverCreds.signedPreKey.keyId,
        publicKey: generateSignalPubKey(receiverCreds.signedPreKey.keyPair.public),
        signature: receiverCreds.signedPreKey.signature },
      preKey: { keyId: 1, publicKey: generateSignalPubKey(preKey.public) },
    } });
    const savedCreds = encode(sender.state.creds);
    senderRepo.close?.();
    sender.close();
    restored = new ParentAuth(senderStore.peer, savedCreds, () => { failures++; });
    restoredRepo = signalRepository(restored);
    assert.equal((await restoredRepo.validateSession(receiverJid)).exists, true);
    const plaintext = Buffer.from('synthetic self-chat payload');
    const encrypted = await restoredRepo.encryptMessage({ jid: receiverJid, data: plaintext });
    const decrypted = await receiverRepo.decryptMessage({ jid: senderJid,
      type: encrypted.type, ciphertext: encrypted.ciphertext });
    assert.deepEqual(decrypted, plaintext);
    const reply = Buffer.from('synthetic encrypted reply');
    const encryptedReply = await receiverRepo.encryptMessage({ jid: senderJid, data: reply });
    const decryptedReply = await restoredRepo.decryptMessage({ jid: receiverJid,
      type: encryptedReply.type, ciphertext: encryptedReply.ciphertext });
    assert.deepEqual(decryptedReply, reply);
    assert.equal(failures, 0);
  } finally {
    senderRepo.close?.(); receiverRepo.close?.(); restoredRepo?.close?.();
    sender.close(); receiver.close(); restored?.close();
    senderStore.peer.close(); receiverStore.peer.close();
  }
});

test('keeps SDK sender-key serialization as bytes through the parent store', async () => {
  const senderStore = new ParentStore();
  const receiverStore = new ParentStore();
  const sender = new ParentAuth(senderStore.peer, null, () => assert.fail('sender store failed'));
  const receiver = new ParentAuth(receiverStore.peer, null, () => assert.fail('receiver store failed'));
  const senderRepo = signalRepository(sender);
  const receiverRepo = signalRepository(receiver);
  const group = '10000000000@g.us';
  const authorJid = '10000000001@s.whatsapp.net';
  const plaintext = Buffer.from('synthetic sender-key payload');
  try {
    const encrypted = await senderRepo.encryptGroupMessage({ group, meId: authorJid, data: plaintext });
    await receiverRepo.processSenderKeyDistributionMessage({ authorJid, item: {
      groupId: group, axolotlSenderKeyDistributionMessage: encrypted.senderKeyDistributionMessage,
    } });
    const decrypted = await receiverRepo.decryptGroupMessage({ group, authorJid, msg: encrypted.ciphertext });
    assert.deepEqual(decrypted, plaintext);
    assert.throws(() => parseKey('session', Buffer.from([1, 2])), z.ZodError);
    assert.throws(() => parseKey('session', { version: 'v1', _sessions: { broken: null } }), z.ZodError);
    assert.throws(() => parseKey('session', { version: 'v1', _sessions: { broken: {} } }), z.ZodError);
  } finally {
    senderRepo.close?.(); receiverRepo.close?.(); sender.close(); receiver.close();
    senderStore.peer.close(); receiverStore.peer.close();
  }
});
