import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { ChildPayload } from './protocol.js';
import { filename, MEDIA_TIMEOUT_MS, MediaFault } from './media_protocol.js';

const descriptor = z.strictObject({ title: z.string().max(4096), description: z.string().max(8192),
  href: z.string().min(1).max(8192), thumb: z.string().max(8192), childnumber: z.number().int().min(0),
  action: z.string().max(4096), params: z.string().max(16384), type: z.string().max(256) });
type Offer = { readonly href: string; readonly kind: 'image' | 'file'; readonly timer: NodeJS.Timeout };
type Media = NonNullable<Extract<ChildPayload, { type: 'message' }>['media']>;

/** Only this expiring map retains provider URLs. Receiving an offer never downloads it. */
export class MediaOffers {
  private readonly values = new Map<string, Offer>();
  private bytes = 0;
  add(messageId: string, msgType: string, content: unknown): Media | undefined {
    if (!['chat.photo', 'share.file', 'chat.voice', 'chat.gif', 'chat.video.msg'].includes(msgType)) return;
    const parsed = descriptor.safeParse(content);
    if (!parsed.success) return;
    const value = parsed.data;
    // The descriptor has scalar fields only, so sorted keys provide canonical full JSON.
    const canonical = JSON.stringify(value, Object.keys(value).sort());
    const sourceId = createHash('sha256').update(canonical).digest('hex');
    const key = JSON.stringify([messageId, sourceId]);
    if (this.values.has(key)) return;
    if (this.values.size >= 1000 || this.bytes + Buffer.byteLength(value.href) > 2 * 1024 * 1024) return;
    const kind = msgType === 'chat.photo' ? 'image' : 'file';
    const fallback = msgType === 'chat.photo' ? 'image.jpg' : msgType === 'chat.gif' ? 'image.gif' : 'attachment.bin';
    const basename = value.title.split(/[\\/]/).at(-1)?.replace(/[^A-Za-z0-9_. -]/g, '_').replace(/\.{2,}/g, '_').slice(0, 180);
    const name = filename.safeParse(basename);
    this.values.set(key, { href: value.href, kind, timer: setTimeout(() => this.remove(key), MEDIA_TIMEOUT_MS) });
    this.bytes += Buffer.byteLength(value.href);
    return { source_id: sourceId, kind, filename: name.success ? name.data : fallback };
  }
  get(messageId: string, sourceId: string): Offer {
    const value = this.values.get(JSON.stringify([messageId, sourceId]));
    if (!value) throw new MediaFault('expired');
    return value;
  }
  private remove(key: string): void {
    const value = this.values.get(key);
    if (!value) return;
    clearTimeout(value.timer);
    this.bytes -= Buffer.byteLength(value.href);
    this.values.delete(key);
  }
  close(): void { for (const key of this.values.keys()) this.remove(key); }
}
