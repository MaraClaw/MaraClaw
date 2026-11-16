import { createHash } from 'node:crypto';
import type { WAMessage } from 'baileys';
import { CHUNK_BYTES } from '../src/media_protocol.js';
import { ControlledTransport, jid } from './support.js';

export const data = Buffer.alloc(2 * CHUNK_BYTES + 31, 0x63);
export function mediaMessage(kind: 'image' | 'file' | 'video' = 'file'): WAMessage {
  const descriptor = { url: 'https://provider.invalid/private-resource', mediaKey: Buffer.alloc(32, 1),
    fileSha256: createHash('sha256').update(data).digest(), fileLength: data.length, caption: 'please read',
    fileName: 'report.docx', mimetype: kind === 'image' ? 'image/png' : kind === 'video' ? 'video/mp4'
      : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', jpegThumbnail: Buffer.from('private-thumbnail') };
  return { key: { id: 'NATIVE-MEDIA', remoteJid: jid, fromMe: true }, messageTimestamp: Math.floor(Date.now() / 1000),
    message: kind === 'image' ? { imageMessage: descriptor } : kind === 'video' ? { videoMessage: descriptor }
      : { documentMessage: descriptor } };
}

export class MediaSocket extends ControlledTransport {
  downloads = 0;
  released = 0;
  readonly signals: AbortSignal[] = [];
  override async *download(message: WAMessage, signal: AbortSignal): AsyncGenerator<Uint8Array> {
    this.downloads++;
    this.signals.push(signal);
    const descriptor = message.message?.documentMessage ?? message.message?.imageMessage ?? message.message?.videoMessage;
    if (descriptor?.jpegThumbnail) throw new Error('thumbnail must never enter the download descriptor');
    try { yield data; } finally { this.released++; }
  }
}
