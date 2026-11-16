import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import type { WAMessage } from 'baileys';
import { notifyText, socketIdentity } from '../src/input.js';
import { CHUNK_BYTES, MEDIA_TIMEOUT_MS, mediaPayload } from '../src/media_protocol.js';
import { MediaOffers } from '../src/media_offers.js';
import { MediaTransport } from '../src/media_transport.js';
import { messagePayload, parentFrame } from '../src/protocol.js';
import type { ChildPayload } from '../src/protocol.js';
import { data, mediaMessage, MediaSocket } from './media-support.js';
import { settle, token } from './support.js';

const identity = socketIdentity({ id: '15551234567@s.whatsapp.net' });
const get = (sourceId: string) => {
  const frame = parentFrame.parse({ type: 'media_get', id: 'get', token,
    transfer_id: 'transfer', message_id: 'NATIVE-MEDIA', source_id: sourceId, limit: data.length });
  if (frame.type !== 'media_get') throw new Error('expected media_get');
  return frame;
};

test('media commands and payloads are strict and cap decoded bytes before base64 allocation', () => {
  const chunk = { type: 'media_chunk', transfer_id: 'transfer', seq: 0, data: Buffer.alloc(CHUNK_BYTES).toString('base64') };
  assert.equal(mediaPayload.safeParse(chunk).success, true);
  for (const extra of [{ data: Buffer.alloc(CHUNK_BYTES + 1).toString('base64') }, { data: 'YR==' },
    { data: 'YQ==\n' }, { seq: -1 }, { seq: true }, { unexpected: 'private' }]) {
    assert.equal(mediaPayload.safeParse({ ...chunk, ...extra }).success, false);
  }
  const command = get('a'.repeat(64));
  assert.equal(parentFrame.safeParse({ ...command, limit: 50 * 1024 * 1024 + 1 }).success, false);
  assert.equal(parentFrame.safeParse({ ...command, url: 'https://private.invalid' }).success, false);
});

for (const kind of ['image', 'file', 'video'] as const) {
  test(`${kind} offers are metadata only, stable across provider URL refresh and bound to the original`, () => {
    const offers = new MediaOffers();
    const message = mediaMessage(kind);
    const [payload] = notifyText({ type: 'notify', messages: [message] }, identity, undefined, offers);
    assert.ok(payload);
    const parsed = messagePayload.parse(payload);
    assert.equal(parsed.media?.kind, kind);
    assert.equal(parsed.text, 'please read');
    const serialized = JSON.stringify(parsed);
    for (const secret of ['provider.invalid', 'mediaKey', 'jpegThumbnail', 'private-thumbnail']) assert.equal(serialized.includes(secret), false);
    const descriptor = message.message?.imageMessage ?? message.message?.documentMessage ?? message.message?.videoMessage;
    assert.ok(descriptor);
    descriptor.url = 'https://provider.invalid/new-url';
    assert.equal(offers.offer(message)?.source_id, parsed.media?.source_id);
    assert.throws(() => offers.take('OTHER-NATIVE-ID', parsed.media?.source_id ?? ''));
  });
}

test('captioned document envelopes preserve filenames and reject quoted, forwarded or mixed content', () => {
  const original = mediaMessage();
  const document = original.message?.documentMessage;
  assert.ok(document);
  const wrapped: WAMessage = { ...original, message: { documentWithCaptionMessage: { message: { documentMessage: document } } } };
  const offers = new MediaOffers();
  const [payload] = notifyText({ type: 'notify', messages: [wrapped] }, identity, undefined, offers);
  assert.ok(payload);
  const parsed = messagePayload.parse(payload);
  assert.equal(parsed.text, 'please read');
  assert.equal(parsed.media?.filename, 'report.docx');
  assert.equal(parsed.media?.kind, 'file');
  assert.equal(offers.offer(wrapped)?.source_id, parsed.media?.source_id);
  for (const contextInfo of [{ isForwarded: true }, { stanzaId: 'quoted' }, { quotedMessage: { conversation: 'quoted' } }]) {
    const message: WAMessage = { ...original, message: { documentWithCaptionMessage: {
      message: { documentMessage: { ...document, contextInfo } },
    } } };
    assert.deepEqual(notifyText({ type: 'notify', messages: [message] }, identity, undefined, offers), []);
  }
  for (const content of [{ documentMessage: document, conversation: 'mixed' }, { imageMessage: document }]) {
    const message: WAMessage = { ...original, message: { documentWithCaptionMessage: { message: content } } };
    assert.deepEqual(notifyText({ type: 'notify', messages: [message] }, identity, undefined, offers), []);
  }
});

test('media preserves self-chat, participant, live and quote/forward restrictions', () => {
  const offers = new MediaOffers();
  const original = mediaMessage();
  for (const key of [{ fromMe: false }, { participant: '15551234567@s.whatsapp.net' }, { remoteJid: 'group@g.us' }]) {
    assert.deepEqual(notifyText({ type: 'notify', messages: [{ ...original, key: { ...original.key, ...key } }] }, identity, undefined, offers), []);
  }
  assert.deepEqual(notifyText({ type: 'append', messages: [original] }, identity, undefined, offers), []);
  const descriptor = original.message?.documentMessage;
  assert.ok(descriptor);
  for (const contextInfo of [{ isForwarded: true }, { stanzaId: 'quoted' }, { quotedMessage: { conversation: 'quoted' } }]) {
    const message: WAMessage = { ...original, message: { documentMessage: { ...descriptor, contextInfo } } };
    assert.deepEqual(notifyText({ type: 'notify', messages: [message] }, identity, undefined, offers), []);
  }
});

test('one transfer waits for each exact ACK, streams multiple chunks, verifies final metadata and releases', async () => {
  const frames: ChildPayload[] = [];
  const socket = new MediaSocket();
  const media = new MediaTransport({ emit: frame => { frames.push(frame); }, current: () => socket, barrier: async () => undefined });
  const offer = media.offers.offer(mediaMessage());
  assert.ok(offer);
  assert.equal(socket.downloads, 0);
  const command = get(offer.source_id);
  assert.equal(command.type, 'media_get');
  if (command.type !== 'media_get') throw new Error('expected media_get');
  media.receive(command);
  await settle();
  assert.equal(frames.length, 1);
  assert.equal(socket.downloads, 1);
  media.receive({ ...command, id: 'duplicate', transfer_id: 'other' });
  const chunks: Buffer[] = [];
  for (let seq = 0; seq < 3; seq++) {
    const frame = frames.find(value => value.type === 'media_chunk' && value.seq === seq);
    assert.ok(frame && frame.type === 'media_chunk');
    const chunk = Buffer.from(frame.data, 'base64');
    assert.ok(chunk.length <= CHUNK_BYTES);
    chunks.push(chunk);
    media.receive({ type: 'media_ack', id: `ack-${seq}`, token, transfer_id: 'transfer', seq });
    await settle();
  }
  assert.deepEqual(Buffer.concat(chunks), data);
  assert.ok(frames.some(frame => frame.type === 'media_end' && frame.seq === 3 && frame.size === data.length &&
    frame.sha256 === createHash('sha256').update(data).digest('hex')));
  assert.ok(frames.some(frame => frame.type === 'media_error' && frame.transfer_id === 'other' && frame.code === 'invalid'));
  assert.equal(socket.released, 1);
  assert.equal(socket.signals[0]?.aborted, true);
  media.close();
});

for (const failure of ['ack', 'revoke', 'limit', 'timeout'] as const) {
  test(`${failure} cancels an active provider stream and leaves the connection ready for a fresh transfer`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const frames: ChildPayload[] = [];
    const socket = new MediaSocket();
    const media = new MediaTransport({ emit: frame => { frames.push(frame); }, current: () => socket, barrier: async () => undefined });
    const message = mediaMessage();
    if (failure === 'limit') { assert.ok(message.message?.documentMessage); message.message.documentMessage.fileLength = 0; }
    const offer = media.offers.offer(message);
    assert.ok(offer);
    const command = get(offer.source_id);
    if (command.type !== 'media_get') throw new Error('expected media_get');
    media.receive({ ...command, limit: failure === 'limit' ? 1 : data.length });
    await settle();
    switch (failure) {
      case 'ack': media.receive({ type: 'media_ack', id: 'bad', token, transfer_id: 'transfer', seq: 1 }); break;
      case 'revoke': media.close(); break;
      case 'limit': break;
      case 'timeout': t.mock.timers.tick(MEDIA_TIMEOUT_MS); break;
    }
    await settle();
    assert.equal(socket.released, 1);
    assert.equal(socket.signals[0]?.aborted, true);
    if (failure !== 'revoke') assert.ok(frames.some(frame => frame.type === 'media_error'));
    const next = media.offers.offer(mediaMessage());
    assert.ok(next);
    media.receive(get(next.source_id));
    await settle();
    assert.equal(socket.downloads, 2);
    media.close();
    await settle();
    assert.equal(socket.released, 2);
  });
}
