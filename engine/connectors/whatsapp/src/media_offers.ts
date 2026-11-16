import { createHash } from 'node:crypto';
import type { WAMessage } from 'baileys';
import { MEDIA_TIMEOUT_MS, MediaFault, mediaOffer } from './media_protocol.js';
import type { MediaOffer } from './media_protocol.js';

type Offer = { readonly message: WAMessage; readonly metadata: MediaOffer; readonly expires: number };

export function originalContent(content: WAMessage['message']): WAMessage['message'] {
  if (!content || !content.documentWithCaptionMessage) return content;
  const fields = Object.keys(content).filter(key => key !== 'messageContextInfo');
  const document = content.documentWithCaptionMessage.message;
  const innerFields = Object.keys(document ?? {}).filter(key => key !== 'messageContextInfo');
  return fields.length === 1 && innerFields.length === 1 && innerFields[0] === 'documentMessage' ? document : null;
}

/** Only bounded original descriptors stay in the worker; URLs, keys and thumbnails never cross stdio. */
export class MediaOffers {
  private readonly entries = new Map<string, Offer>();
  offer(message: WAMessage): MediaOffer | null {
    const content = originalContent(message.message);
    const media = content?.imageMessage ?? content?.documentMessage ?? content?.videoMessage;
    if (!media || !message.key.id || !media.mediaKey || media.mediaKey.length !== 32 ||
      !media.fileSha256 || media.fileSha256.length !== 32 || (media.fileEncSha256?.length ?? 32) !== 32 ||
      typeof media.url !== 'string' || media.url.length === 0 || media.url.length > 8192 ||
      (media.directPath?.length ?? 0) > 8192) return null;
    const kind = content?.imageMessage ? 'image' : content?.videoMessage ? 'video' : 'file';
    const contentType = media.mimetype || (kind === 'image' ? 'image/jpeg' : kind === 'video' ? 'video/mp4' : 'application/octet-stream');
    const extension = new Map([['image/png', 'png'], ['image/webp', 'webp'], ['image/gif', 'gif'], ['video/webm', 'webm']]).get(contentType)
      ?? (kind === 'image' ? 'jpg' : kind === 'video' ? 'mp4' : 'bin');
    const filename = content?.documentMessage?.fileName || `${kind}.${extension}`;
    if (filename.length > 2048 || contentType.length > 512) return null;
    const length = media.fileLength;
    const size = typeof length === 'number' ? length : length?.toNumber() ?? 0;
    const descriptor = { url: media.url, directPath: media.directPath ?? null, mediaKey: Uint8Array.from(media.mediaKey),
      fileSha256: Uint8Array.from(media.fileSha256), fileEncSha256: media.fileEncSha256 ? Uint8Array.from(media.fileEncSha256) : null,
      fileLength: size, mimetype: contentType };
    const sourceId = createHash('sha256').update(JSON.stringify([message.key.id, kind, filename, contentType, size,
      Buffer.from(media.fileSha256).toString('base64')])).digest('hex');
    const parsed = mediaOffer.safeParse({ source_id: sourceId, kind, filename, content_type: contentType, size });
    if (!parsed.success) return null;
    for (const [id, offer] of this.entries) if (offer.expires <= Date.now()) this.entries.delete(id);
    if (this.entries.size >= 128 && !this.entries.has(sourceId)) return null;
    const slim: WAMessage = { key: { ...message.key }, message: kind === 'image' ? { imageMessage: descriptor }
      : kind === 'video' ? { videoMessage: descriptor } : { documentMessage: descriptor } };
    this.entries.set(sourceId, { message: slim, metadata: parsed.data, expires: Date.now() + MEDIA_TIMEOUT_MS });
    return parsed.data;
  }
  take(messageId: string, sourceId: string): Offer {
    const offer = this.entries.get(sourceId);
    if (!offer || offer.message.key.id !== messageId) throw new MediaFault('invalid');
    this.entries.delete(sourceId);
    if (offer.expires <= Date.now()) throw new MediaFault('expired');
    return offer;
  }
  clear(): void { this.entries.clear(); }
}
