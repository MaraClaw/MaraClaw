import { z } from 'zod'
import type { ChatAttachment } from './message.ts'

export type ChatSubmission = {
  readonly client_submission_id: string
  readonly content: string
  readonly display_content?: string
  readonly file_name?: string
  readonly attachments?: readonly ChatAttachment[]
  readonly model_id?: string
  readonly kind?: never
}

export type ChatOutbound = ChatSubmission | { readonly kind: 'onboarding_trigger' }

const eventSchema = z.object({
  type: z.string(), content: z.string().optional(), session_id: z.string().optional(),
  message_id: z.string().optional(), entry_id: z.string().optional(),
  attachments: z.array(z.object({ path: z.string().min(1), name: z.string().min(1) })).optional(),
  name: z.string().optional(), status: z.string().optional(), result: z.string().optional(),
  role: z.string().optional(), arguments: z.string().optional(), agent_id: z.string().optional(),
  env: z.string().optional(), output: z.string().optional(),
  live_preview: z.object({
    env: z.string().optional(), screenshot_url: z.string().optional(), output: z.string().optional(),
  }).optional(),
})

const turnAcceptedSchema = eventSchema.extend({
  type: z.literal('turn_accepted'),
  client_submission_id: z.uuid(), message_id: z.uuid(), session_id: z.uuid(),
})

const turnRejectedSchema = eventSchema.extend({
  type: z.literal('turn_rejected'),
  client_submission_id: z.uuid(),
  reason: z.enum(['quota_exceeded', 'agent_expired', 'no_company_model', 'enqueue_failed',
    'invalid_submission_id', 'submission_conflict', 'invalid_attachments', 'openclaw_update_required', 'openclaw_update_in_progress']),
  content: z.string(),
  notice_id: z.uuid().optional(),
})

const onboardingRejectedSchema = turnRejectedSchema.extend({
  client_submission_id: z.undefined().optional(),
  reason: turnRejectedSchema.shape.reason.exclude(['invalid_submission_id', 'submission_conflict', 'invalid_attachments']),
})

export const inboundSchema = z.union([
  turnAcceptedSchema, turnRejectedSchema, onboardingRejectedSchema,
  eventSchema.refine((event) => event.type !== 'turn_rejected' && event.type !== 'turn_accepted'),
])

export type ChatInbound = z.infer<typeof inboundSchema>
export type ChatTurnAcceptance = z.infer<typeof turnAcceptedSchema>
export type ChatTurnRejection = z.infer<typeof turnRejectedSchema> | z.infer<typeof onboardingRejectedSchema>

export function isTurnAccepted(event: ChatInbound): event is ChatTurnAcceptance {
  return event.type === 'turn_accepted'
}

export function isTurnRejected(event: ChatInbound): event is ChatTurnRejection {
  return event.type === 'turn_rejected'
}
