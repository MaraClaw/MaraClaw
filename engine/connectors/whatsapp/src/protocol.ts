import { z } from "zod";
import type { Diagnostic } from "./diagnostics.js";
import { mediaCommands, mediaOffer } from './media_protocol.js';
import type { MediaPayload } from './media_protocol.js';

export const MAX_FRAME_BYTES = 1024 * 1024;
export const jsonValue = z.json();
export type Json = z.infer<typeof jsonValue>;
export const jsonObject = z.record(z.string().max(512), jsonValue);
const correlation = z.string().min(1).max(256);
const envelope = { id: correlation, token: z.string().min(1).max(512) };
const leaseMs = z.number().int().min(1).max(120_000);
export const parentFrame = z.discriminatedUnion("type", [
  z.strictObject({ ...envelope, type: z.literal("init"), creds: z.string().nullable(), lease_ms: leaseMs }),
  z.strictObject({ ...envelope, type: z.literal("lease"), lease_ms: leaseMs }),
  z.strictObject({ ...envelope, type: z.literal("shutdown") }),
  z.strictObject({ ...envelope, type: z.literal("result"), ok: z.boolean(), value: jsonValue, error: z.string().max(256).nullable() }),
  z.strictObject({ ...envelope, type: z.literal("send"), command_id: correlation,
    message_id: correlation, self_jid: z.string().min(1).max(256),
    text: z.string().min(1).max(4096), timeout_ms: z.number().int().min(1).max(120_000) }),
  ...mediaCommands.map(schema => schema.extend(envelope)),
]);
export type ParentFrame = Readonly<z.infer<typeof parentFrame>>;
export type SendCommand = Extract<ParentFrame, { type: "send" }>;
export type CloseReason = "logged_out" | "replaced" | "forbidden" | "transport_failed" | "auth_store_failed" | "lease_expired" | "shutdown";
export type SendOutcome = "accepted" | "rejected" | "uncertain";
export type AuthBatch = { readonly category: string; readonly values: Readonly<Record<string, Json>> };
export const messagePayload = z.strictObject({ type: z.literal('message'), message_id: correlation,
  remote_jid: correlation, participant: z.string().nullable(), from_me: z.boolean(), timestamp: z.int().min(0).max(Number.MAX_SAFE_INTEGER),
  text: z.string().refine(value => Array.from(value).length <= 4096), event_kind: z.literal('notify'),
  is_history: z.literal(false), media: mediaOffer.nullable().optional() }).refine(value => value.text.length > 0 || value.media != null);
export type ChildPayload =
  | Diagnostic
  | MediaPayload
  | { readonly type: "auth_get"; readonly category: string; readonly ids: readonly string[] }
  | { readonly type: "auth_set"; readonly batches: readonly AuthBatch[] }
  | { readonly type: "creds_set"; readonly creds: string }
  | { readonly type: "ready" }
  | { readonly type: "qr"; readonly value: string; readonly expires_at: string }
  | { readonly type: "identity"; readonly account_id: string; readonly pn_jid: string | null; readonly lid_jid: string | null }
  | Readonly<z.infer<typeof messagePayload>>
  | { readonly type: "closed"; readonly reason: CloseReason }
  | { readonly type: "send_result"; readonly command_id: string; readonly message_id: string; readonly outcome: SendOutcome };
export type ChildFrame = ChildPayload & { readonly id: string; readonly token: string };
export class BridgeFault extends Error {
  override readonly name = "BridgeFault";
  constructor(readonly code: CloseReason = "transport_failed") { super(code); }
}
export function assertNever(_value: never): never { throw new BridgeFault(); }

/** Incremental byte framing bounds even an unterminated line before parsing JSON. */
export class Frames {
  private pending = Buffer.alloc(0);
  push(chunk: Buffer): readonly ParentFrame[] {
    const frames: ParentFrame[] = [];
    let start = 0;
    for (let end = chunk.indexOf(10); end !== -1; end = chunk.indexOf(10, start)) {
      if (this.pending.length + end - start > MAX_FRAME_BYTES) throw new BridgeFault();
      const line = Buffer.concat([this.pending, chunk.subarray(start, end)]);
      this.pending = Buffer.alloc(0);
      const text = new TextDecoder("utf-8", { fatal: true }).decode(line);
      const raw: unknown = JSON.parse(text);
      frames.push(parentFrame.parse(raw));
      start = end + 1;
    }
    if (this.pending.length + chunk.length - start > MAX_FRAME_BYTES) throw new BridgeFault();
    this.pending = Buffer.concat([this.pending, chunk.subarray(start)]);
    return frames;
  }
}
