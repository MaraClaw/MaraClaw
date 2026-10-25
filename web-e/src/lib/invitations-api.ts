import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { joinOrgResponseSchema } from './types/auth.ts'

export const invitationPreviewSchema = z.object({
  tenant: z.object({ id: z.uuid(), name: z.string().min(1) }).readonly(),
}).readonly()
export type InvitationPreview = z.infer<typeof invitationPreviewSchema>

export async function previewInvitation(code: string, context: { readonly owner: SessionOwner; readonly signal: AbortSignal }) {
  return apiRequest('/api/tenants/invitations/preview', {
    schema: invitationPreviewSchema, method: 'POST', body: { invitation_code: code }, owner: context.owner, signal: context.signal,
  })
}

export async function joinInvitation(code: string, owner: SessionOwner) {
  return apiRequest('/api/tenants/join', {
    schema: joinOrgResponseSchema, method: 'POST', body: { invitation_code: code }, owner,
  })
}
