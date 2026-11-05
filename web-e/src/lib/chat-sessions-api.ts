import { authSession, type SessionOwner } from './auth-session.ts'
import { SessionOutSchema, SessionPageOutSchema } from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

export type SessionListScope = 'mine' | 'all'
export const SESSION_PAGE_SIZE = 50

type SessionRequest = {
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export async function listSessionsPage(
  agentId: string,
  scope: SessionListScope,
  request: SessionRequest & { readonly cursor?: string | null | undefined; readonly limit?: number } = {},
) {
  const params = new URLSearchParams({ scope, limit: String(request.limit ?? SESSION_PAGE_SIZE) })
  if (request.cursor != null) params.set('cursor', request.cursor)
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/sessions/page?${params}`, {
    schema: SessionPageOutSchema, owner: request.owner ?? authSession.capture(),
    ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}

export async function getSession(agentId: string, sessionId: string, request: SessionRequest = {}) {
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}`, {
    schema: SessionOutSchema, owner: request.owner ?? authSession.capture(),
    ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}
