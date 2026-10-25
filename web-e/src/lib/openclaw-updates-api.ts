import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'

const noticeSchema = z.object({
  id: z.uuid(),
  title: z.string(),
  body: z.string(),
  mandatory: z.boolean(),
  created_at: z.iso.datetime({ offset: true }),
}).readonly()

const runSchema = z.object({
  id: z.uuid(),
  state: z.enum(['pending', 'cutover', 'succeeded', 'failed']),
  failure_code: z.string().nullable(),
}).readonly()

export const agentUpdateSchema = z.object({
  notice: noticeSchema.nullable(),
  run: runSchema.nullable(),
  required: z.boolean(),
  can_upgrade: z.boolean(),
}).readonly()

const detailSchema = z.object({
  notice: noticeSchema,
  agents: z.array(z.object({
    agent_id: z.uuid(), name: z.string(), status: z.string(), update: agentUpdateSchema,
  }).readonly()).readonly(),
}).readonly()

export type OpenClawNotice = z.infer<typeof noticeSchema>
export type OpenClawRun = z.infer<typeof runSchema>
export type AgentUpdate = z.infer<typeof agentUpdateSchema>

type RequestContext = {
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

function requestContext(owner?: SessionOwner, signal?: AbortSignal): RequestContext {
  return {
    ...(owner === undefined ? {} : { owner }),
    ...(signal === undefined ? {} : { signal }),
  }
}

export async function getOpenClawUpdate(noticeId: string, context: RequestContext = {}) {
  const { owner, signal } = context
  return apiRequest(`/api/openclaw-updates/${encodeURIComponent(noticeId)}`, { schema: detailSchema, ...requestContext(owner, signal) })
}

export async function getAgentOpenClawUpdate(agentId: string, context: RequestContext = {}) {
  const { owner, signal } = context
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/openclaw-update`, { schema: agentUpdateSchema, ...requestContext(owner, signal) })
}

export async function upgradeAgentOpenClaw(
  agentId: string, noticeId: string, context: RequestContext = {},
): Promise<OpenClawRun> {
  const { owner, signal } = context
  const body = z.object({ notice_id: z.uuid() }).parse({ notice_id: noticeId })
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/openclaw-update`, {
    schema: runSchema, ...requestContext(owner, signal), method: 'POST', body,
  })
}
