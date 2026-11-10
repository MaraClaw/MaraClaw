import { CollaborationAcknowledgementSchema, HumanRelationshipPageResponseSchema } from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

export function listRelationshipPage(agentId: string, options: {
  readonly cursor: string | null
  readonly signal?: AbortSignal
}) {
  const query = new URLSearchParams({ limit: '50' })
  if (options.cursor !== null) query.set('cursor', options.cursor)
  return apiRequest(`/api/agents/${agentId}/relationships/page?${query}`, {
    schema: HumanRelationshipPageResponseSchema, ...(options.signal === undefined ? {} : { signal: options.signal }),
  })
}

export function addRelationship(agentId: string, body: {
  readonly member_id: string
  readonly relation: string
  readonly description: string
}) {
  return apiRequest(`/api/agents/${agentId}/relationships/`, {
    method: 'POST', body, schema: CollaborationAcknowledgementSchema,
  })
}
