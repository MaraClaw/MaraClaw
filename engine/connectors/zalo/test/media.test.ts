import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import dns from 'node:dns/promises';
import https from 'node:https';
import type { RequestOptions } from 'node:https';
import { ClientRequest, IncomingMessage } from 'node:http';
import { Socket } from 'node:net';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import { ThreadType, UserMessage } from 'zca-js';
import { MAX_FRAME_BYTES, parentFrame, childFrame } from '../src/protocol.js';
import type { ChildFrame } from '../src/protocol.js';
import { Harness, credentials, messageData, token } from './support.js';

const bytes = Buffer.alloc(1024 * 1024 + 333, 37);
const sha256 = createHash('sha256').update(bytes).digest('hex');
const descriptor = { title: '../report.bin', description: '', href: 'https://media.example/path/full?secret=provider',
  thumb: 'https://thumb.invalid/private', childnumber: 0, action: '', params: '', type: '' };
const common = { id: 'media', token };

test('strict current offers download over 1 MiB in bounded acknowledged frames with private URLs retained in child', async context => {
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  let requests = 0;
  context.mock.method(dns, 'lookup', async () => [{ address: '8.8.8.8', family: 4 }]);
  context.mock.method(https, 'get', (_url: URL, options: RequestOptions, callback: (response: IncomingMessage) => void) => {
    requests++;
    assert.equal(options.agent, false);
    assert.deepEqual(options.headers, { 'accept-encoding': 'identity' });
    assert.ok(options.lookup);
    options.lookup('media.example', { family: 4 }, (error, address, family) => {
      assert.equal(error, null); assert.equal(address, '8.8.8.8'); assert.equal(family, 4);
    });
    const request = new ClientRequest({ createConnection: () => new Socket() });
    queueMicrotask(() => {
      const response = new IncomingMessage(new Socket());
      response.statusCode = 200;
      response.headers = { 'content-length': String(bytes.length) };
      callback(response);
      for (let offset = 0; offset < bytes.length; offset += 16_384) response.push(bytes.subarray(offset, offset + 16_384));
      response.push(null);
      request.destroy();
    });
    return request;
  });
  h.runtime.receive({ ...common, type: 'init', lease_ms: 120000, credentials: JSON.stringify(credentials),
    media_allowed_hosts: ['media.example'] });
  h.ack((await h.waitFor('creds_set')).id);
  await h.waitFor('ready');
  h.sdk.api.listener.emit('message', new UserMessage('10001', messageData({ msgType: 'share.file', content: descriptor })));
  const offer = await h.waitFor('message');
  assert.ok(offer.media);
  assert.equal(offer.text, '');
  assert.equal(requests, 0);
  assert.equal(offer.media.source_id, createHash('sha256').update(JSON.stringify(descriptor, Object.keys(descriptor).sort())).digest('hex'));
  assert.equal(offer.media.filename, 'report.bin');
  const chunks: Buffer[] = [];
  h.events.on('frame', frame => {
    if (frame.type !== 'media_chunk') return;
    const data = Buffer.from(frame.data, 'base64');
    assert.ok(data.length <= 65536);
    assert.equal(frame.seq, chunks.length);
    chunks.push(data);
    h.runtime.receive({ ...common, type: 'media_ack', transfer_id: frame.transfer_id, seq: frame.seq });
  });
  h.runtime.receive({ ...common, type: 'media_get', transfer_id: 'inbound', message_id: offer.message_id,
    source_id: offer.media.source_id, limit: 50 * 1024 * 1024 });
  const end = await h.waitFor('media_end');
  assert.equal(requests, 1);
  assert.deepEqual(Buffer.concat(chunks), bytes);
  assert.equal(end.size, bytes.length);
  assert.equal(end.sha256, sha256);
  for (const frame of h.frames) assert.ok(Buffer.byteLength(JSON.stringify(frame)) < MAX_FRAME_BYTES);
  assert.equal(JSON.stringify(h.frames).includes('provider'), false);
  assert.equal(JSON.stringify(h.frames).includes(descriptor.href), false);
});

test('attachment-only Buffer dispatch accepts message:null once and leaves credential ACK, lease and heartbeat responsive', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const h = new Harness();
  context.after(() => h.runtime.stop('shutdown'));
  h.init(JSON.stringify(credentials), 60_000);
  h.ack((await h.waitFor('creds_set')).id);
  await h.waitFor('ready');
  h.sdk.api.response = async () => ({ message: null, attachment: [{ msgId: 931 }] });
  h.runtime.receive({ ...common, type: 'media_put', transfer_id: 'out', size: bytes.length, sha256 });
  assert.equal((await h.waitFor('media_ready')).phase, 'receiving');
  for (let seq = 0, offset = 0; offset < bytes.length; offset += 65536, seq++) {
    const after = h.frames.length;
    h.runtime.receive(parentFrame.parse({ ...common, type: 'media_chunk', transfer_id: 'out', seq,
      data: bytes.subarray(offset, offset + 65536).toString('base64') }));
    assert.equal((await h.waitFor('media_ack', after)).seq, seq);
    if (seq === 0) {
      const cookie = h.sdk.api.cookies[0];
      assert.ok(cookie);
      cookie.value = 'refreshed-credential';
      h.runtime.receive({ ...common, type: 'lease', lease_ms: 120000 });
      context.mock.timers.tick(60_000);
      await setImmediate();
      h.ack((await h.waitFor('creds_set', after)).id);
      assert.equal((await h.waitFor('heartbeat', after)).ok, true);
    }
  }
  const after = h.frames.length;
  h.runtime.receive({ ...common, type: 'media_end', transfer_id: 'out', size: bytes.length, sha256 });
  assert.equal((await h.waitFor('media_ready', after)).phase, 'complete');
  const command = parentFrame.parse({ ...common, type: 'send', command_id: 'send-once', thread_id: '20002', text: '',
    attachment: { transfer_id: 'out', filename: 'report.bin', kind: 'file', size: bytes.length, sha256 } });
  h.runtime.receive(command);
  const result = await h.waitFor('send_result');
  assert.equal(result.outcome, 'accepted');
  assert.equal(result.message_id, '931');
  h.runtime.receive(command);
  await setImmediate();
  assert.equal(h.sdk.api.dispatched.length, 1);
  const dispatched = h.sdk.api.dispatched[0];
  assert.equal(dispatched?.type, ThreadType.User);
  assert.ok(dispatched && typeof dispatched.text !== 'string');
  const attachment = dispatched.text.attachments;
  assert.ok(attachment && !Array.isArray(attachment) && typeof attachment !== 'string');
  assert.ok('data' in attachment);
  assert.deepEqual(attachment.data, bytes);
  assert.equal(attachment.metadata.totalSize, bytes.length);
  for (const frame of h.frames) assert.ok(Buffer.byteLength(JSON.stringify(frame)) < MAX_FRAME_BYTES);
});

test('canonical chunk and single attachment contracts reject malformed or overlarge frames', () => {
  for (const data of ['AAAA=', 'AA', '____', Buffer.alloc(65537).toString('base64')]) {
    assert.equal(childFrame.safeParse({ ...common, type: 'media_chunk', transfer_id: 't', seq: 0, data }).success, false);
    assert.equal(parentFrame.safeParse({ ...common, type: 'media_chunk', transfer_id: 't', seq: 0, data }).success, false);
  }
  const empty: ChildFrame = childFrame.parse({ ...common, type: 'message', message_id: 'm', sender_id: '2',
    thread_id: '2', from_me: false, timestamp: 1, text: '', media: { source_id: sha256, kind: 'file', filename: 'file.bin' } });
  assert.equal(empty.type, 'message');
  assert.equal(parentFrame.safeParse({ ...common, type: 'send', command_id: 'c', thread_id: '2', text: '' }).success, false);
});
