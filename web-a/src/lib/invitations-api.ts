import { z } from 'zod'

import { ApiError, apiRequest } from './http.ts'

export const INVITATIONS_PAGE_SIZE = 20
export const INVITATION_TTL_HOURS = 336
export const invitationEmailSchema = z.string().trim().toLowerCase().pipe(z.email('Enter a valid email address.'))
const ttlSchema = z.number({ error: 'Enter a whole number of hours.' }).int('Enter a whole number of hours.')
  .min(1, 'Use at least 1 hour.').max(INVITATION_TTL_HOURS, 'Use 336 hours (14 days) or fewer.')
export const inviteUsersSchema = z.object({
  emails: z.array(invitationEmailSchema).min(1), ttl_hours: ttlSchema.default(INVITATION_TTL_HOURS),
}).readonly()
export const invitationIdSchema = z.string().min(1).brand<'InvitationId'>()
const timestampSchema = z.iso.datetime({ offset: true, local: true })
export const invitationCodeSchema = z.object({
  id: invitationIdSchema, code: z.string().min(1), max_uses: z.int().nonnegative(),
  used_count: z.int().nonnegative(), is_active: z.boolean(), created_at: timestampSchema,
  expires_at: timestampSchema.nullable(), invited_email: z.string().nullable(),
  redeemed_at: timestampSchema.nullable(), status: z.enum(['active', 'expired', 'used', 'revoked']),
}).readonly()
export const invitationPageSchema = z.object({
  items: z.array(invitationCodeSchema).readonly(), total: z.int().nonnegative(),
  page: z.int().positive(), page_size: z.int().positive(),
}).readonly()
const inviteResultSchema = z.object({ invited: z.int().nonnegative(), message: z.string() }).readonly()

export type InviteUsersRequest = z.infer<typeof inviteUsersSchema>
export type InvitationCode = z.infer<typeof invitationCodeSchema>
export type InvitationId = z.infer<typeof invitationIdSchema>
export type InvitationPage = z.infer<typeof invitationPageSchema>

export async function sendInvitations(payload: InviteUsersRequest) {
  return apiRequest('/api/enterprise/invite-users', {
    schema: inviteResultSchema,
    body: inviteUsersSchema.parse(payload),
  })
}

export async function fetchInvitationCodes(page: number, signal?: AbortSignal) {
  const query = new URLSearchParams({ page: String(z.int().positive().parse(page)), page_size: String(INVITATIONS_PAGE_SIZE) })
  return apiRequest(`/api/enterprise/invitation-codes?${query}`, { schema: invitationPageSchema, signal })
}

export async function revokeInvitation(id: InvitationId) {
  const invitationId = invitationIdSchema.parse(id)
  await apiRequest<void>(`/api/enterprise/invitation-codes/${encodeURIComponent(invitationId)}`, { method: 'DELETE' })
}

export function invitationSendFailure(error: Error, canConfigureSMTP: boolean) {
  if (error instanceof ApiError && /smtp|mail.*(?:disabled|not configured|not enabled)|(?:disabled|not configured).*mail/i.test(error.message)) {
    return canConfigureSMTP
      ? { message: 'Organization email is disabled or not configured. Configure organization SMTP in Settings, then try again.', settings: true }
      : { message: 'Organization email is disabled or not configured. Ask your organization’s genesis administrator to configure SMTP in Settings, then try again.', settings: false }
  }
  return { message: 'Could not confirm the invitation was created. Check the history before trying again.', settings: false }
}
