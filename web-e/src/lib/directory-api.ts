import { z } from 'zod'

import { authSession, type SessionOwner } from './auth-session.ts'
import { AuthUserSchema } from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

const directoryUserSchema = AuthUserSchema.strip().readonly()
const pageSchema = z.object({
  items: z.array(directoryUserSchema).readonly(),
  next_cursor: z.string().min(1).nullable(),
}).readonly()

export type DirectoryUser = z.infer<typeof directoryUserSchema>
type PageRequest = {
  readonly search?: string
  readonly cursor?: string | null
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export async function listOrgUsersPage(request: PageRequest = {}) {
  const params = new URLSearchParams({ search: (request.search ?? '').trim().toLowerCase(), limit: '50' })
  if (request.cursor != null) params.set('cursor', request.cursor)
  return apiRequest(`/api/org/users/page?${params}`, {
    schema: pageSchema,
    owner: request.owner ?? authSession.capture(),
    ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}
