import { z } from 'zod'

import { apiRequest, type RequestOptions } from './http.ts'
import { personalStatusSchema, type PersonalCommand } from './whatsapp-api.ts'

const generationSchema = z.uuid()
const diagnosticTimeSchema = z.iso.datetime({ offset: true }).nullable().default(null)
const zaloDiagnosticsSchema = z.object({
  reconnect_attempts: z.int().min(0).max(1000).default(0),
  retry_at: diagnosticTimeSchema,
  last_error: z.enum(['account_in_use', 'identity_ambiguous', 'protocol_error', 'login_failed', 'auth_store_failed', 'transport_failed', 'qr_expired', 'qr_declined', 'lease_expired', 'session_replaced']).nullable().default(null),
  last_error_at: diagnosticTimeSchema,
  connected_at: diagnosticTimeSchema,
  last_activity_at: diagnosticTimeSchema,
  heartbeat_at: diagnosticTimeSchema,
  heartbeat_failures: z.int().min(0).max(1000).default(0),
}).readonly()
export const zaloPersonalStatusSchema = personalStatusSchema.unwrap().extend({
  qr: z.object({
    value: z.string().min(1).max(768000).regex(/^iVBORw0KGgo[A-Za-z0-9+/]*={0,2}$/, 'Expected a base64 PNG'),
    expires_at: z.iso.datetime(),
  }).readonly().nullable(),
  error_code: z.string().nullable(),
  resumable: z.boolean().default(false),
  diagnostics: zaloDiagnosticsSchema.nullable().default(null),
}).readonly()
export type ZaloPersonalStatus = z.infer<typeof zaloPersonalStatusSchema>
type PersonalContext = Required<Pick<RequestOptions, 'owner' | 'signal'>>

export function getZaloPersonalStatus(agentId: string, context: PersonalContext): Promise<ZaloPersonalStatus> {
  return apiRequest(`/api/agents/${agentId}/zalo-channel/personal`, {
    owner: context.owner, signal: context.signal, cache: 'no-store', schema: zaloPersonalStatusSchema,
  })
}

export function changeZaloPersonalConnection(agentId: string, command: PersonalCommand, context: PersonalContext): Promise<ZaloPersonalStatus> {
  switch (command.kind) {
    case 'start':
      return apiRequest(`/api/agents/${agentId}/zalo-channel/personal/start`, {
        owner: context.owner, signal: context.signal, method: 'POST', cache: 'no-store', schema: zaloPersonalStatusSchema,
        body: { expected_generation: generationSchema.nullable().parse(command.generation) },
      })
    case 'relink':
      return apiRequest(`/api/agents/${agentId}/zalo-channel/personal/relink`, {
        owner: context.owner, signal: context.signal, method: 'POST', cache: 'no-store', schema: zaloPersonalStatusSchema,
        body: { expected_generation: generationSchema.parse(command.generation) },
      })
    case 'disconnect':
      return apiRequest(`/api/agents/${agentId}/zalo-channel/personal?expected_generation=${encodeURIComponent(generationSchema.parse(command.generation))}`, {
        owner: context.owner, signal: context.signal, method: 'DELETE', cache: 'no-store', schema: zaloPersonalStatusSchema,
      })
    default: return assertNever(command)
  }
}

function assertNever(value: never): never { throw new TypeError(`Unknown personal operation: ${typeof value}`) }
