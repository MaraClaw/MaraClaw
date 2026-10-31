import { z } from 'zod'

import { ApiError, apiRequest, type RequestOptions } from './http.ts'

const generationSchema = z.uuid()
const utcSchema = z.iso.datetime()
export const personalStatusSchema = z.object({
  mode: z.literal('personal_qr'),
  generation: generationSchema.nullable(),
  attempt_id: z.uuid().nullable(),
  revision: z.number().int().nonnegative(),
  state: z.enum(['pending', 'qr', 'connected', 'reconnecting', 'paused', 'expired', 'disconnecting', 'disconnected', 'failed']),
  expires_at: utcSchema.nullable(),
  qr: z.object({ value: z.string().min(1).max(8192), expires_at: utcSchema }).readonly().nullable(),
  account_hint: z.string().nullable(),
  error_code: z.enum(['logged_out', 'account_in_use', 'identity_ambiguous', 'auth_store_failed', 'transport_failed']).nullable(),
}).readonly()
export type PersonalStatus = z.infer<typeof personalStatusSchema>
export type WhatsAppMode = 'cloud' | 'personal_qr'
type PersonalContext = Required<Pick<RequestOptions, 'owner' | 'signal'>>
export type PersonalCommand =
  | { readonly kind: 'start'; readonly generation: string | null }
  | { readonly kind: 'relink' | 'disconnect'; readonly generation: string }

/** Read only the discriminator: generic credentials never enter this query's cache. */
export async function getWhatsAppMode(agentId: string, context: PersonalContext): Promise<WhatsAppMode> {
  try {
    const config = await apiRequest(`/api/agents/${agentId}/whatsapp-channel`, {
      owner: context.owner, signal: context.signal, cache: 'no-store', schema: z.object({ mode: z.enum(['cloud', 'personal_qr']).default('cloud') }),
    })
    return config.mode
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return 'cloud'
    throw error
  }
}

export function getPersonalStatus(agentId: string, context: PersonalContext): Promise<PersonalStatus> {
  return apiRequest(`/api/agents/${agentId}/whatsapp-channel/personal`, {
    owner: context.owner, signal: context.signal, cache: 'no-store', schema: personalStatusSchema,
  })
}

export function changePersonalConnection(agentId: string, command: PersonalCommand, context: PersonalContext): Promise<PersonalStatus> {
  switch (command.kind) {
    case 'start':
      return apiRequest(`/api/agents/${agentId}/whatsapp-channel/personal/start`, {
        owner: context.owner, signal: context.signal, method: 'POST', cache: 'no-store', schema: personalStatusSchema,
        body: { expected_generation: generationSchema.nullable().parse(command.generation) },
      })
    case 'relink':
      return apiRequest(`/api/agents/${agentId}/whatsapp-channel/personal/relink`, {
        owner: context.owner, signal: context.signal, method: 'POST', cache: 'no-store', schema: personalStatusSchema,
        body: { expected_generation: generationSchema.parse(command.generation) },
      })
    case 'disconnect':
      return apiRequest(`/api/agents/${agentId}/whatsapp-channel/personal?expected_generation=${encodeURIComponent(generationSchema.parse(command.generation))}`, {
        owner: context.owner, signal: context.signal, method: 'DELETE', cache: 'no-store', schema: personalStatusSchema,
      })
    default:
      return assertNever(command)
  }
}

function assertNever(value: never): never { throw new TypeError(`Unknown personal operation: ${typeof value}`) }
