import assert from 'node:assert/strict';
import dns from 'node:dns/promises';
import https from 'node:https';
import type { RequestOptions } from 'node:https';
import { ClientRequest, IncomingMessage } from 'node:http';
import { Socket } from 'node:net';
import { test } from 'node:test';
import { downloadMedia, mediaUrl, publicAddress } from '../src/media_download.js';
import { MediaFault } from '../src/media_protocol.js';
import { MediaOffers } from '../src/media_offers.js';
import { MediaTransport } from '../src/media_transport.js';
import type { ChildPayload } from '../src/protocol.js';

const cookies = { getCookieString: async (_url: string) => '', setCookie: async (_cookie: string, _url: string) => undefined };
const signal = new AbortController().signal;
test('only exact HTTPS hosts and globally routable DNS answers are eligible, including mapped IPv6 refusal', async context => {
  let requests = 0;
  context.mock.method(https, 'get', () => { requests++; assert.fail('forbidden download fetched'); });
  for (const value of ['http://media.example/a', 'https://media.example:444/a', 'https://u:p@media.example/a',
    'https://127.0.0.1/a', 'https://[::1]/a', 'https://media.example.evil/a', 'https://media.example/a#secret']) {
    assert.throws(() => mediaUrl(value, ['media.example']), { code: 'forbidden' });
  }
  for (const address of ['0.0.0.0', '10.0.0.1', '100.100.100.200', '127.0.0.1', '169.254.169.254', '168.63.129.16',
    '172.16.0.1', '192.168.1.1', '198.18.0.1', '224.0.0.1', '::1', 'fe80::1', 'fc00::1',
    '::ffff:127.0.0.1', '::ffff:7f00:1', '::ffff:a00:1', '2001:db8::1']) assert.equal(publicAddress(address), false, address);
  assert.equal(publicAddress('8.8.8.8'), true);
  assert.equal(publicAddress('2606:4700:4700::1111'), true);
  await assert.rejects(downloadMedia('https://media.example/a', 100, { hosts: [], cookies, signal }), { code: 'forbidden' });
  context.mock.method(dns, 'lookup', async () => [{ address: '8.8.8.8', family: 4 }, { address: '10.0.0.1', family: 4 }]);
  await assert.rejects(downloadMedia('https://media.example/a', 100, { hosts: ['media.example'], cookies, signal }), { code: 'forbidden' });
  assert.equal(requests, 0);
});

test('redirects revalidate the full URL and cookie jar per hop, cap streamed bytes and redact failures', async context => {
  context.mock.method(dns, 'lookup', async () => [{ address: '8.8.8.8', family: 4 }]);
  const visited: string[] = [];
  const lookedUp: string[] = [];
  const set: string[] = [];
  let scenario = 'success';
  context.mock.method(https, 'get', (url: URL, options: RequestOptions, callback: (response: IncomingMessage) => void) => {
    visited.push(url.href);
    assert.deepEqual(options.headers, { 'accept-encoding': 'identity', cookie: `cookie-for:${url.href}` });
    const request = new ClientRequest({ createConnection: () => new Socket() });
    queueMicrotask(() => {
      const response = new IncomingMessage(new Socket());
      const redirect = url.pathname === '/first';
      response.statusCode = redirect ? 302 : 200;
      response.headers = redirect ? { location: scenario === 'external' ? 'https://evil.example/private' : '/second?token=private',
        'set-cookie': ['session=secret; Path=/'] } : {};
      if (scenario === 'encoded') response.headers['content-encoding'] = 'gzip';
      callback(response);
      response.push(Buffer.from(scenario === 'oversize' ? '123456' : 'abc'));
      response.push(null);
      request.destroy();
    });
    return request;
  });
  const jar = { getCookieString: async (url: string) => { lookedUp.push(url); return `cookie-for:${url}`; },
    setCookie: async (cookie: string, url: string) => { set.push(`${url}:${cookie}`); } };
  const contextValue = { hosts: ['media.example'], cookies: jar, signal };
  assert.deepEqual(await downloadMedia('https://media.example/first', 5, contextValue), Buffer.from('abc'));
  assert.deepEqual(lookedUp, ['https://media.example/first', 'https://media.example/second?token=private']);
  assert.deepEqual(set, ['https://media.example/first:session=secret; Path=/']);
  scenario = 'external';
  await assert.rejects(downloadMedia('https://media.example/first', 5, contextValue), { code: 'forbidden', message: 'forbidden' });
  assert.equal(visited.includes('https://evil.example/private'), false);
  scenario = 'oversize';
  await assert.rejects(downloadMedia('https://media.example/direct', 5, contextValue), { code: 'too_large' });
  scenario = 'encoded';
  await assert.rejects(downloadMedia('https://media.example/direct', 5, contextValue), { code: 'download_failed' });
});

test('unknown attachment shapes are skipped and bounded stages reject sequence, digest and deadline violations', context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const offers = new MediaOffers();
  context.after(() => offers.close());
  const content = { title: 'a.bin', description: '', href: 'https://media.example/file', thumb: '', childnumber: 0,
    action: '', params: '', type: '' };
  assert.equal(offers.add('quoted', 'share.file', { quote: content }), undefined);
  assert.equal(offers.add('thumb', 'chat.photo', { ...content, href: '' }), undefined);
  assert.equal(offers.add('unknown', 'unknown', content), undefined);
  assert.equal(offers.add('extra', 'share.file', { ...content, future: 'unknown' }), undefined);
  assert.equal(offers.add('animation', 'chat.gif', content)?.kind, 'file');
  const offer = offers.add('current', 'share.file', content);
  assert.ok(offer);
  context.mock.timers.tick(120000);
  assert.throws(() => offers.get('current', offer.source_id), MediaFault);
  const frames: ChildPayload[] = [];
  const media = new MediaTransport({ emit: frame => frames.push(frame), cookies: () => cookies, hosts: [], allowed: () => true });
  context.after(() => media.close());
  const common = { id: 'i', token: 't' };
  const sha256 = 'a'.repeat(64);
  for (const transfer_id of ['1', '2', '3']) media.receive({ ...common, type: 'media_put', transfer_id, size: 3, sha256 });
  assert.deepEqual(frames.at(-1), { type: 'media_error', transfer_id: '3', code: 'too_large' });
  media.receive({ ...common, type: 'media_chunk', transfer_id: '1', seq: 1, data: 'YWJj' });
  assert.deepEqual(frames.at(-1), { type: 'media_error', transfer_id: '1', code: 'invalid' });
  media.receive({ ...common, type: 'media_chunk', transfer_id: '2', seq: 0, data: 'YWJj' });
  media.receive({ ...common, type: 'media_end', transfer_id: '2', size: 3, sha256 });
  assert.deepEqual(frames.at(-1), { type: 'media_error', transfer_id: '2', code: 'digest_mismatch' });
  media.receive({ ...common, type: 'media_put', transfer_id: 'fresh', size: 3, sha256 });
  context.mock.timers.tick(120000);
  assert.deepEqual(frames.at(-1), { type: 'media_error', transfer_id: 'fresh', code: 'expired' });
});
