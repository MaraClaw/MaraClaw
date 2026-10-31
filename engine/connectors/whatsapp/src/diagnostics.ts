import { z } from 'zod';
import type { Peer } from './peer.js';

const categorySchema = z.enum(['pre-key', 'session', 'sender-key', 'sender-key-memory', 'app-state-sync-key',
  'app-state-sync-version', 'lid-mapping', 'device-list', 'tctoken', 'identity-key', 'creds', 'other']);
const issueSchema = z.enum(['invalid_type', 'too_big', 'too_small', 'invalid_format', 'not_multiple_of',
  'unrecognized_keys', 'invalid_union', 'invalid_key', 'invalid_element', 'invalid_value', 'custom']);
const dropReasonSchema = z.enum(['not_notify', 'identity_unavailable', 'non_self', 'missing_content', 'stub',
  'content_shape', 'text', 'quoted_or_forwarded', 'timestamp']);
export type DropReason = z.infer<typeof dropReasonSchema>;
export const diagnosticSchema = z.strictObject({
  type: z.literal('diagnostic'),
  stage: z.enum(['init', 'auth_initial_decode', 'auth_read', 'auth_key_decode', 'auth_write_encode', 'auth_rpc',
    'connection', 'reconnect', 'identity', 'intake', 'send', 'stop', 'bridge_eof', 'worker_exit']),
  event: z.enum(['start', 'committed', 'failure', 'update', 'finished']),
  operation: z.enum(['auth_get', 'auth_set', 'creds_set']).optional(),
  category: categorySchema.optional(), categories: z.array(categorySchema).max(12).optional(), rpc_id: z.uuid().optional(),
  count: z.int().min(0).max(1000000).optional(), dropped: z.int().min(0).max(1000000).optional(),
  drop_reasons: z.array(dropReasonSchema).max(9).optional(), event_kind: z.enum(['notify', 'append']).optional(),
  has_request_id: z.boolean().optional(),
  elapsed_ms: z.int().min(0).max(86400000).optional(),
  retry_count: z.int().min(0).max(1000).optional(), delay_ms: z.int().min(0).max(86400000).optional(),
  error_kind: z.enum(['validation', 'json', 'timeout', 'parent_rejected', 'closed', 'other']).optional(),
  value_shape: z.enum(['null', 'bytes', 'array', 'object', 'string', 'number', 'boolean', 'other']).optional(),
  issue_code: issueSchema.optional(), status_code: z.int().min(0).max(999).optional(),
  exit_code: z.int().min(-255).max(255).optional(),
  connection_state: z.enum(['connecting', 'open', 'close', 'unknown']).optional(),
  qr_available: z.boolean().optional(), identity_accepted: z.boolean().optional(),
  outcome: z.enum(['accepted', 'rejected', 'uncertain']).optional(),
  close_reason: z.enum(['logged_out', 'replaced', 'forbidden', 'transport_failed', 'auth_store_failed',
    'lease_expired', 'shutdown']).optional(),
});
export type Diagnostic = Readonly<z.infer<typeof diagnosticSchema>>;
export function safeCategory(category: string): z.infer<typeof categorySchema> {
  const parsed = categorySchema.safeParse(category);
  return parsed.success ? parsed.data : 'other';
}
function valueShape(value: unknown): NonNullable<Diagnostic['value_shape']> {
  if (value === null) return 'null';
  if (value instanceof Uint8Array) return 'bytes';
  if (Array.isArray(value)) return 'array';
  const kind = typeof value;
  switch (kind) {
    case 'object': return 'object';
    case 'string': return 'string';
    case 'number': return 'number';
    case 'boolean': return 'boolean';
    case 'undefined': case 'function': case 'symbol': case 'bigint': return 'other';
  }
  const exhaustive: never = kind;
  return exhaustive;
}
export function failure(error: unknown, value?: unknown): Pick<Diagnostic, 'error_kind' | 'issue_code' | 'value_shape'> {
  const shape = value === undefined ? {} : { value_shape: valueShape(value) };
  if (error instanceof z.ZodError) {
    const code = error.issues[0]?.code;
    return { ...shape, error_kind: 'validation', ...(code ? { issue_code: code } : {}) };
  }
  return { ...shape, error_kind: error instanceof SyntaxError ? 'json' : 'other' };
}
export function diagnostic(peer: Peer | undefined, payload: Diagnostic): boolean {
  const parsed = diagnosticSchema.safeParse(payload);
  if (!parsed.success || !peer) return false;
  try { peer.emit(parsed.data); return true; }
  catch { return false; } // A closed observation pipe must not change authentication or shutdown.
}
