import { z } from 'zod'

import { apiRequest, type RequestOptions } from './http.ts'
import { personalStatusSchema, type PersonalCommand } from './whatsapp-api.ts'

const generationSchema = z.uuid()
export const zaloPersonalStatusSchema = personalStatusSchema.unwrap().extend({
  qr: z.object({
    value: z.string().min(1).max(768000).regex(/^iVBORw0KGgo[A-Za-z0-9+/]*={0,2}$/, 'Expected a base64 PNG'),
    expires_at: z.iso.datetime(),
  }).readonly().nullable(),
  error_code: z.string().nullable(),
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
