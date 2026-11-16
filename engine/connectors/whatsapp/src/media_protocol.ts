import { z } from 'zod';

export const CHUNK_BYTES = 64 * 1024;
export const FILE_BYTES = 50 * 1024 * 1024;
export const IMAGE_BYTES = 10 * 1024 * 1024;
export const MEDIA_TIMEOUT_MS = 120_000;
const correlation = z.string().min(1).max(256);
const sequence = z.int().min(0).max(FILE_BYTES);
const digest = z.string().length(64).regex(/^[a-f0-9]{64}$/);
export const mediaCode = z.enum(['invalid', 'expired', 'forbidden', 'too_large', 'download_failed', 'digest_mismatch']);
export type MediaCode = z.infer<typeof mediaCode>;
export class MediaFault extends Error {
  override readonly name = 'MediaFault';
  constructor(readonly code: MediaCode) { super(code); }
}
export const mediaOffer = z.strictObject({ source_id: digest, kind: z.enum(['image', 'file', 'video']),
  filename: z.string().min(1).refine(value => value.length <= 2048 && Array.from(value).length <= 1024),
  content_type: z.string().min(1).refine(value => value.length <= 512 && Array.from(value).length <= 256),
  size: z.int().min(0).max(Number.MAX_SAFE_INTEGER) });
export type MediaOffer = Readonly<z.infer<typeof mediaOffer>>;
export const mediaCommands = [
  z.strictObject({ type: z.literal('media_get'), transfer_id: correlation, message_id: correlation,
    source_id: digest, limit: z.int().min(1).max(FILE_BYTES) }),
  z.strictObject({ type: z.literal('media_ack'), transfer_id: correlation, seq: sequence }),
  z.strictObject({ type: z.literal('media_error'), transfer_id: correlation, code: mediaCode }),
] as const;
const chunkData = z.string().min(4).max(87384).regex(/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/)
  .refine(value => {
    const size = value.length / 4 * 3 - (value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0);
    return size > 0 && size <= CHUNK_BYTES && Buffer.from(value, 'base64').toString('base64') === value;
  });
export const mediaPayload = z.discriminatedUnion('type', [
  z.strictObject({ type: z.literal('media_chunk'), transfer_id: correlation, seq: sequence, data: chunkData }),
  z.strictObject({ type: z.literal('media_end'), transfer_id: correlation, seq: sequence,
    size: z.int().min(1).max(FILE_BYTES), sha256: digest }),
  mediaCommands[2],
]);
export type MediaPayload = Readonly<z.infer<typeof mediaPayload>>;
