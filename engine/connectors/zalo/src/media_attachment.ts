import sharp from 'sharp';
import type { Metadata } from 'sharp';
import type { AttachmentSource } from 'zca-js';
import type { SendCommand } from './protocol.js';
import { MediaFault } from './media_protocol.js';

export async function bufferAttachment(command: SendCommand, data: Buffer): Promise<AttachmentSource> {
  const attachment = command.attachment;
  if (!attachment) throw new MediaFault('invalid');
  const parts = /^(.*)\.([A-Za-z0-9]{1,16})$/.exec(attachment.filename);
  const stem = parts?.[1];
  const extension = parts?.[2];
  if (!stem || !extension || extension.toLowerCase() === 'gif' || data.subarray(0, 3).toString() === 'GIF') {
    throw new MediaFault('unsupported');
  }
  if (attachment.kind === 'image') {
    let metadata: Metadata;
    try { metadata = await sharp(data, { animated: false, limitInputPixels: 40_000_000 }).metadata(); }
    catch (error) { if (error instanceof Error) throw new MediaFault('unsupported'); throw error; }
    if (!metadata.width || !metadata.height || (metadata.pages ?? 1) !== 1 ||
      !['jpeg', 'png', 'webp'].includes(metadata.format) || !['jpg', 'jpeg', 'png', 'webp'].includes(extension.toLowerCase())) {
      throw new MediaFault('unsupported');
    }
    return { data, filename: `${stem}.${extension}`, metadata: { totalSize: data.length,
      width: metadata.width, height: metadata.height } };
  }
  if (['jpg', 'jpeg', 'png', 'webp'].includes(extension.toLowerCase())) throw new MediaFault('unsupported');
  return { data, filename: `${stem}.${extension}`, metadata: { totalSize: data.length } };
}
