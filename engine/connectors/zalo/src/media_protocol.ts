import { isIP } from 'node:net';
import { z } from 'zod';

export const CHUNK_BYTES = 65_536;
export const MEDIA_TIMEOUT_MS = 120_000;
export const FILE_BYTES = 50 * 1024 * 1024;
export const IMAGE_BYTES = 10 * 1024 * 1024;
export const digest = z.string().regex(/^[a-f0-9]{64}$/);
export const filename = z.string().min(3).max(180).regex(/^[A-Za-z0-9_][A-Za-z0-9_. -]*\.[A-Za-z0-9]{1,16}$/)
  .refine(value => !value.includes('..'));
export const host = z.string().max(253).regex(/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/)
  .refine(value => isIP(value) === 0);
export const offer = z.strictObject({ source_id: digest, kind: z.enum(['image', 'file']), filename });
export const attachment = z.strictObject({ transfer_id: z.string().min(1).max(256), filename,
  kind: z.enum(['image', 'file']), size: z.number().int().min(1).max(FILE_BYTES), sha256: digest })
  .refine(value => value.size <= (value.kind === 'image' ? IMAGE_BYTES : FILE_BYTES));
export const mediaCode = z.enum(['invalid', 'expired', 'forbidden', 'too_large', 'download_failed', 'digest_mismatch', 'unsupported']);
export type MediaCode = z.infer<typeof mediaCode>;
export class MediaFault extends Error {
  override readonly name = 'MediaFault';
  constructor(readonly code: MediaCode) { super(code); }
}
export const transfer = { transfer_id: z.string().min(1).max(256) };
export const chunk = { ...transfer, seq: z.number().int().min(0).max(800),
  data: z.string().min(4).max(87_384).refine(value => {
    const bytes = Buffer.from(value, 'base64');
    return bytes.length > 0 && bytes.length <= CHUNK_BYTES && bytes.toString('base64') === value;
  }) };
export const end = { ...transfer, size: z.number().int().min(1).max(FILE_BYTES), sha256: digest };
