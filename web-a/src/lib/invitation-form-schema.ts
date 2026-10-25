import { z } from 'zod'

import { invitationEmailSchema, type InviteUsersRequest } from './invitations-api.ts'

export const INVITATION_TTL_DAYS = 14
export const invitationDraftSchema = z.object({
  emails: z.string().transform((value, context) => {
    const parsed = z.array(invitationEmailSchema).safeParse(value.split(';'))
    if (!parsed.success) {
      context.addIssue({ code: 'custom', message: 'Enter valid email addresses separated by semicolons, with no empty entries.' })
      return z.NEVER
    }
    return [...new Set(parsed.data)]
  }),
  ttl_days: z.number({ error: 'Enter a whole number of days.' }).int('Enter a whole number of days.')
    .min(1, 'Use at least 1 day.').max(INVITATION_TTL_DAYS, 'Use 14 days or fewer.'),
}).readonly()
export type InvitationDraftInput = z.input<typeof invitationDraftSchema>
export type InvitationDraft = z.infer<typeof invitationDraftSchema>

export function invitationRequest(draft: InvitationDraft): InviteUsersRequest {
  return { emails: draft.emails, ttl_hours: draft.ttl_days * 24 }
}
