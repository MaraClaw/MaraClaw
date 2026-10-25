import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'

const agentSetupSchema = z.object({
  setup_pending: z.boolean(),
  state: z.enum(['pending', 'running', 'succeeded', 'failed']).nullable(),
  failure_code: z.string().nullable(),
  retryable: z.boolean(),
}).readonly()

export type AgentSetup = z.infer<typeof agentSetupSchema>
type RequestContext = { readonly owner?: SessionOwner; readonly signal?: AbortSignal }

export async function getAgentSetup(agentId: string, context: RequestContext = {}): Promise<AgentSetup> {
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/setup`, {
    schema: agentSetupSchema, method: 'GET',
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}

export async function retryAgentSetup(agentId: string, context: RequestContext = {}): Promise<AgentSetup> {
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/setup/retry`, {
    schema: agentSetupSchema, method: 'POST',
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}
