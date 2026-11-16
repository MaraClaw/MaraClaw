import assert from 'node:assert/strict';
import dns from 'node:dns/promises';
import https from 'node:https';
import type { RequestOptions } from 'node:https';
import { ClientRequest, IncomingMessage } from 'node:http';
import { Socket } from 'node:net';
import { mock } from 'node:test';
import { UserMessage } from 'zca-js';
import { Frames, childFrame } from '../src/protocol.js';
import { Runtime } from '../src/runtime.js';
import { TestSdk, messageData } from './support.js';

const data = Buffer.alloc(1024 * 1024 + 333, 37);
const sdk = new TestSdk();
mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
mock.method(dns, 'lookup', async () => [{ address: '8.8.8.8', family: 4 }]);
mock.method(https, 'get', (url: URL, _options: RequestOptions, callback: (response: IncomingMessage) => void) => {
  assert.equal(url.pathname, '/allowed');
  const request = new ClientRequest({ createConnection: () => new Socket() });
  queueMicrotask(() => {
    const response = new IncomingMessage(new Socket());
    response.statusCode = 200;
    response.headers = { 'content-length': String(data.length) };
    callback(response);
    for (let offset = 0; offset < data.length; offset += 16384) response.push(data.subarray(offset, offset + 16384));
    response.push(null);
    request.destroy();
  });
  return request;
});
sdk.api.response = async () => {
  assert.equal(sdk.api.dispatched.length, 1);
  const payload = sdk.api.dispatched[0]?.text;
  assert.ok(payload && typeof payload !== 'string');
  assert.equal(payload.msg, '');
  const attachment = payload.attachments;
  assert.ok(attachment && !Array.isArray(attachment) && typeof attachment !== 'string' && 'data' in attachment);
  assert.deepEqual(attachment.data, data);
  return { message: null, attachment: [{ msgId: 90001 }] };
};
let heartbeat = false;
const runtime = new Runtime({ client: () => sdk, exit: () => {
  mock.timers.reset(); process.stdin.destroy();
}, write: line => {
  process.stdout.write(line);
  const frame = childFrame.parse(JSON.parse(line));
  if (frame.type === 'ready') {
    for (const message_id of ['blocked', 'allowed']) sdk.api.listener.emit('message', new UserMessage('10001', messageData({
      msgId: message_id, msgType: 'share.file', content: { title: 'file.bin', description: '',
        href: `https://media.example/${message_id}`, thumb: '', childnumber: 0, action: '', params: '', type: '' } })));
  }
  if (frame.type === 'media_chunk' && !heartbeat) {
    heartbeat = true;
    const cookie = sdk.api.cookies[0];
    assert.ok(cookie);
    cookie.value = 'private-refresh';
    mock.timers.tick(60000);
  }
} });
const frames = new Frames();
process.stdin.on('data', (bytes: Buffer) => { for (const frame of frames.push(bytes)) runtime.receive(frame); });
process.stdin.on('end', () => runtime.stop('transport_failed'));
