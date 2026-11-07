import { authSession, type SessionOwner } from './auth-session.ts'
import { FileInfoPageSchema } from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

export const FILE_PAGE_SIZE = 100

export function listFilesPage(
  agentId: string,
  path = '',
  request: {
    readonly cursor?: string | null | undefined
    readonly limit?: number
    readonly owner?: SessionOwner
    readonly signal?: AbortSignal
  } = {},
) {
  const params = new URLSearchParams({ path, limit: String(request.limit ?? FILE_PAGE_SIZE) })
  if (request.cursor != null) params.set('cursor', request.cursor)
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/files/page?${params}`, {
    schema: FileInfoPageSchema,
    owner: request.owner ?? authSession.capture(),
    ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}
