import dns from 'node:dns/promises';
import https from 'node:https';
import { BlockList, isIP } from 'node:net';
import type { IncomingMessage } from 'node:http';
import { MediaFault } from './media_protocol.js';

export interface MediaCookies {
  getCookieString(url: string): Promise<string>;
  setCookie(cookie: string, url: string): Promise<unknown>;
}
const blocked = new BlockList();
for (const [address, prefix] of [['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8],
  ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24], ['192.0.2.0', 24], ['192.168.0.0', 16],
  ['198.18.0.0', 15], ['198.51.100.0', 24], ['203.0.113.0', 24], ['224.0.0.0', 3]] satisfies [string, number][]) {
  blocked.addSubnet(address, prefix, 'ipv4');
}
blocked.addSubnet('2001::', 23, 'ipv6');
blocked.addSubnet('2001:db8::', 32, 'ipv6');
blocked.addSubnet('2002::', 16, 'ipv6');
blocked.addAddress('168.63.129.16', 'ipv4');
const globalV6 = new BlockList();
globalV6.addSubnet('2000::', 3, 'ipv6');

export function publicAddress(address: string): boolean {
  switch (isIP(address)) {
    case 4: return !blocked.check(address, 'ipv4');
    case 6: return globalV6.check(address, 'ipv6') && !blocked.check(address, 'ipv6');
    default: return false;
  }
}
export function mediaUrl(value: string, hosts: readonly string[]): URL {
  let url: URL;
  try { url = new URL(value); }
  catch (error) { if (error instanceof TypeError) throw new MediaFault('forbidden'); throw error; }
  if (url.protocol !== 'https:' || (url.port !== '' && url.port !== '443') || url.username || url.password ||
    url.hash || isIP(url.hostname.replace(/^\[|\]$/g, '')) !== 0 || !hosts.includes(url.hostname)) {
    throw new MediaFault('forbidden');
  }
  return url;
}
type Download = { readonly hosts: readonly string[]; readonly cookies: MediaCookies; readonly signal: AbortSignal };

export async function downloadMedia(href: string, limit: number, context: Download): Promise<Buffer> {
  let current = href;
  try {
    for (let hop = 0; hop <= 3; hop++) {
      context.signal.throwIfAborted();
      const url = mediaUrl(current, context.hosts);
      let abort: (() => void) | undefined;
      const addresses = await Promise.race([dns.lookup(url.hostname, { all: true, verbatim: true }),
        new Promise<never>((_resolve, reject) => {
          abort = () => reject(new MediaFault('expired'));
          context.signal.addEventListener('abort', abort, { once: true });
          if (context.signal.aborted) abort();
        }),
      ]).finally(() => { if (abort) context.signal.removeEventListener('abort', abort); });
      if (addresses.length === 0 || addresses.some(item => !publicAddress(item.address))) throw new MediaFault('forbidden');
      const pinned = addresses[0];
      if (!pinned) throw new MediaFault('forbidden');
      const cookie = await context.cookies.getCookieString(url.href);
      context.signal.throwIfAborted();
      const response = await new Promise<IncomingMessage>((resolve, reject) => {
        const request = https.get(url, { signal: context.signal, agent: false, family: pinned.family, maxHeaderSize: 16_384,
          headers: { 'accept-encoding': 'identity', ...(cookie ? { cookie } : {}) },
          lookup: (_hostname, _options, callback) => callback(null, pinned.address, pinned.family),
        }, resolve);
        request.setTimeout(15_000, () => request.destroy(new MediaFault('download_failed')));
        request.on('error', reject);
      });
      try {
        for (const value of response.headers['set-cookie'] ?? []) await context.cookies.setCookie(value, url.href);
        const status = response.statusCode ?? 0;
        if ([301, 302, 303, 307, 308].includes(status)) {
          const location = response.headers.location;
          if (!location || hop === 3) throw new MediaFault('forbidden');
          current = mediaUrl(new URL(location, url).href, context.hosts).href;
          continue;
        }
        if (status !== 200 || (response.headers['content-encoding'] ?? 'identity') !== 'identity') {
          throw new MediaFault('download_failed');
        }
        const length = response.headers['content-length'];
        if (length !== undefined && (!/^\d+$/.test(length) || Number(length) > limit)) throw new MediaFault('too_large');
        const data = Buffer.alloc(limit);
        let size = 0;
        for await (const chunk of response) {
          if (!Buffer.isBuffer(chunk)) throw new MediaFault('download_failed');
          if (size + chunk.length > limit) throw new MediaFault('too_large');
          chunk.copy(data, size);
          size += chunk.length;
        }
        if (size === 0) throw new MediaFault('download_failed');
        return data.subarray(0, size);
      } finally { response.destroy(); }
    }
    throw new MediaFault('forbidden');
  } catch (error) {
    if (error instanceof MediaFault) throw error;
    throw new MediaFault(context.signal.aborted ? 'expired' : 'download_failed');
  }
}
