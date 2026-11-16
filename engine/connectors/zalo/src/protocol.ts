import { z } from 'zod';
import { attachment, chunk, end, FILE_BYTES, host, mediaCode, offer, transfer } from './media_protocol.js';

export const MAX_FRAME_BYTES = 1024 * 1024;
export const MAX_TEXT_CHARS = 2000;
export const MAX_LEASE_MS = 120_000;
const correlation = z.string().min(1).max(256);
const providerId = z.string().regex(/^[0-9]{1,64}$/);
const envelope = { id: correlation, token: z.string().min(1).max(512) };
const lease = z.number().int().min(1).max(MAX_LEASE_MS);
export const textSchema = z.string().min(1).max(MAX_TEXT_CHARS * 2)
  .refine(value => value.trim().length > 0 && [...value].length <= MAX_TEXT_CHARS);
export const parentFrame = z.discriminatedUnion('type', [
  z.strictObject({ ...envelope, type: z.literal('init'), lease_ms: lease, credentials: z.string().max(768_000).nullable(),
    keepalive_interval_ms: z.number().int().min(60_000).max(1_800_000).optional(),
    media_allowed_hosts: z.array(host).max(100).optional(), media_transfer_timeout_ms: z.literal(120_000).optional() }),
  z.strictObject({ ...envelope, type: z.literal('lease'), lease_ms: lease }),
  z.strictObject({ ...envelope, type: z.literal('shutdown') }),
  z.strictObject({ ...envelope, type: z.literal('result'), request_id: correlation, ok: z.boolean() }),
  z.strictObject({ ...envelope, type: z.literal('send'), command_id: correlation, thread_id: providerId,
    text: z.union([textSchema, z.literal('')]), attachment: attachment.optional() })
    .refine(value => value.text.length > 0 || value.attachment !== undefined),
  z.strictObject({ ...envelope, type: z.literal('typing'), thread_id: providerId }),
  z.strictObject({ ...envelope, type: z.literal('media_get'), ...transfer, message_id: correlation,
    source_id: offer.shape.source_id, limit: z.number().int().min(1).max(FILE_BYTES) }),
  z.strictObject({ ...envelope, type: z.literal('media_put'), ...end }),
  z.strictObject({ ...envelope, type: z.literal('media_chunk'), ...chunk }),
  z.strictObject({ ...envelope, type: z.literal('media_end'), ...end }),
  z.strictObject({ ...envelope, type: z.literal('media_ack'), ...transfer, seq: chunk.seq }),
  z.strictObject({ ...envelope, type: z.literal('media_error'), ...transfer, code: mediaCode }),
]);
export const closeReason = z.enum(['shutdown', 'lease_expired', 'protocol_error', 'login_failed',
  'auth_store_failed', 'transport_failed', 'qr_expired', 'qr_declined', 'session_replaced']);
export type CloseReason = z.infer<typeof closeReason>;
const png = z.string().max(768_000).regex(/^[A-Za-z0-9+/]+={0,2}$/).refine(value => {
  const bytes = Buffer.from(value, 'base64');
  return bytes.toString('base64') === value && bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'));
});
export const childFrame = z.discriminatedUnion('type', [
  z.strictObject({ ...envelope, type: z.literal('ready') }),
  z.strictObject({ ...envelope, type: z.literal('heartbeat'), ok: z.boolean() }),
  z.strictObject({ ...envelope, type: z.literal('qr'), value: png, expires_at: z.iso.datetime() }),
  z.strictObject({ ...envelope, type: z.literal('creds_set'), creds: z.string().min(1).max(768_000) }),
  z.strictObject({ ...envelope, type: z.literal('identity'), account_id: providerId }),
  z.strictObject({ ...envelope, type: z.literal('message'), message_id: correlation, sender_id: providerId,
    thread_id: providerId, from_me: z.boolean(), timestamp: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
    text: z.string().max(131_072), media: offer.optional() })
    .refine(value => value.text.trim().length > 0 || value.media !== undefined),
  z.strictObject({ ...envelope, type: z.literal('closed'), reason: closeReason }),
  z.strictObject({ ...envelope, type: z.literal('send_result'), command_id: correlation,
    message_id: z.string().max(256), outcome: z.enum(['accepted', 'rejected', 'uncertain']) }),
  z.strictObject({ ...envelope, type: z.literal('media_chunk'), ...chunk }),
  z.strictObject({ ...envelope, type: z.literal('media_end'), ...end }),
  z.strictObject({ ...envelope, type: z.literal('media_ack'), ...transfer, seq: chunk.seq }),
  z.strictObject({ ...envelope, type: z.literal('media_ready'), ...transfer, phase: z.enum(['receiving', 'complete']) }),
  z.strictObject({ ...envelope, type: z.literal('media_error'), ...transfer, code: mediaCode }),
]);
export type ParentFrame = Readonly<z.infer<typeof parentFrame>>;
export type ChildFrame = Readonly<z.infer<typeof childFrame>>;
export type ChildPayload = ChildFrame extends infer Frame ? Frame extends ChildFrame ? Omit<Frame, 'id' | 'token'> : never : never;
export type SendCommand = Extract<ParentFrame, { type: 'send' }>;
export type SendResult = Extract<ChildPayload, { type: 'send_result' }>;
export class BridgeFault extends Error {
  override readonly name = 'BridgeFault';
  constructor(readonly reason: CloseReason) { super(reason); }
}
export function assertNever(_value: never): never { throw new BridgeFault('protocol_error'); }

/** Raw bytes are bounded before decoding or parsing, including unterminated input. */
export class Frames {
  private readonly pending: Buffer[] = [];
  private bytes = 0;
  *push(chunk: Buffer): Generator<ParentFrame> {
    let start = 0;
    for (let end = chunk.indexOf(10); end !== -1; end = chunk.indexOf(10, start)) {
      if (this.bytes + end - start > MAX_FRAME_BYTES) throw new BridgeFault('protocol_error');
      const line = Buffer.concat([...this.pending, chunk.subarray(start, end)]);
      this.pending.length = 0;
      this.bytes = 0;
      const raw: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
      yield parentFrame.parse(raw);
      start = end + 1;
    }
    this.bytes += chunk.length - start;
    if (this.bytes > MAX_FRAME_BYTES) throw new BridgeFault('protocol_error');
    if (start < chunk.length) this.pending.push(chunk.subarray(start));
  }
}
