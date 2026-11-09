import { z } from 'zod'

import { ApiError, apiRequest } from './http.ts'
import {
  BroadcastRequestSchema, NotificationBroadcastEmailCancellationResponseSchema,
  NotificationBroadcastEmailStatusResponseSchema, NotificationBroadcastResponseSchema,
  NotificationBroadcastHistoryItemSchema, NotificationBroadcastHistoryPageResponseSchema,
  NotificationBroadcastJobAcceptanceResponseSchema, NotificationBroadcastJobStatusResponseSchema,
} from './engine-contracts.generated.ts'

export const broadcastDraftSchema = BroadcastRequestSchema.strip().extend({
  title: z.string().max(200, 'Use at most 200 characters.').refine((value) => value.trim().length > 0, 'Enter a title.'),
  body: z.string().max(1000, 'Use at most 1,000 characters.'),
  send_email: z.boolean(),
})

export type BroadcastDraft = Readonly<z.infer<typeof broadcastDraftSchema>>

export const broadcastIdSchema = z.uuid().brand<'BroadcastId'>()
export type BroadcastId = z.infer<typeof broadcastIdSchema>

const broadcastResultSchema = NotificationBroadcastResponseSchema.strip().extend({
  ok: z.literal(true),
  broadcast_id: broadcastIdSchema,
  users_notified: z.number().int().nonnegative(),
  agents_notified: z.number().int().nonnegative(),
  emails_sent: z.number().int().nonnegative(),
}).readonly()

export type BroadcastResult = z.infer<typeof broadcastResultSchema>

const jobAudienceFields = {
  broadcast_id: broadcastIdSchema,
  accepted_at: z.iso.datetime({ offset: true }),
  users_total: z.number().int().nonnegative(),
  agents_total: z.number().int().nonnegative(),
}
const broadcastAcceptanceSchema = NotificationBroadcastJobAcceptanceResponseSchema.strip().extend(jobAudienceFields).readonly()
const broadcastJobStatusSchema = NotificationBroadcastJobStatusResponseSchema.strip().extend({
  ...jobAudienceFields,
  completed_at: z.iso.datetime({ offset: true }).nullable(),
  processed: z.number().int().nonnegative(),
  users_notified: z.number().int().nonnegative(),
  agents_notified: z.number().int().nonnegative(),
  skipped: z.number().int().nonnegative(),
  emails_queued: z.number().int().nonnegative(),
}).refine((value) => (
  value.users_notified <= value.users_total && value.agents_notified <= value.agents_total &&
  value.processed <= value.users_total + value.agents_total &&
  value.processed === value.users_notified + value.agents_notified + value.skipped &&
  value.emails_queued <= value.users_notified &&
  (value.status === 'completed'
    ? value.completed_at !== null && value.processed === value.users_total + value.agents_total
    : value.completed_at === null)
)).readonly()

export type BroadcastAcceptance = z.infer<typeof broadcastAcceptanceSchema>
export type BroadcastJobStatus = z.infer<typeof broadcastJobStatusSchema>

export async function acceptBroadcast(payload: BroadcastDraft, idempotencyKey: string): Promise<BroadcastAcceptance> {
  return apiRequest('/api/notifications/broadcast/jobs', {
    schema: broadcastAcceptanceSchema, method: 'POST', body: payload, idempotencyKey,
  })
}

export async function fetchBroadcastJobStatus(id: BroadcastId, signal?: AbortSignal): Promise<BroadcastJobStatus> {
  return apiRequest(`/api/notifications/broadcast/jobs/${encodeURIComponent(id)}`, {
    schema: broadcastJobStatusSchema.refine((value) => value.broadcast_id === id), signal,
  })
}

const broadcastHistoryItemSchema = NotificationBroadcastHistoryItemSchema.strip().extend({
  broadcast_id: broadcastIdSchema,
  published_at: z.iso.datetime({ offset: true }),
  users_notified: z.number().int().nonnegative(),
  agents_notified: z.number().int().nonnegative(),
  emails_sent: z.number().int().nonnegative(),
}).readonly()
const broadcastHistoryPageSchema = NotificationBroadcastHistoryPageResponseSchema.strip().extend({
  items: z.array(broadcastHistoryItemSchema).max(20).readonly(),
  next_cursor: z.string().min(1).nullable(),
}).readonly()

export async function fetchBroadcastHistory(cursor: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams({ limit: '20' })
  if (cursor !== null) params.set('cursor', cursor)
  return apiRequest(`/api/notifications/broadcast/history?${params}`, { schema: broadcastHistoryPageSchema, signal })
}

/** A reviewed draft with the key that makes resending it safe. */
export type BroadcastSubmission = Readonly<{ draft: BroadcastDraft; idempotencyKey: string }>

export async function sendBroadcast(payload: BroadcastDraft, idempotencyKey: string): Promise<BroadcastResult> {
  return apiRequest('/api/notifications/broadcast', {
    schema: broadcastResultSchema,
    method: 'POST',
    body: payload,
    idempotencyKey,
  })
}

function sameDraft(left: BroadcastDraft, right: BroadcastDraft): boolean {
  return left.title === right.title && left.body === right.body && left.send_email === right.send_email
}

/** Reuse the key of an unconfirmed attempt at this exact draft; any other draft gets a fresh key. */
export function reviewBroadcast(
  draft: BroadcastDraft,
  attempt: BroadcastSubmission | null,
  mintKey: () => string = () => crypto.randomUUID(),
): BroadcastSubmission {
  return Object.freeze({ draft, idempotencyKey: attempt && sameDraft(attempt.draft, draft) ? attempt.idempotencyKey : mintKey() })
}

/** The attempt to keep for a retry after a failure; a 409 means the server tied the key to another broadcast. */
export function attemptAfterFailure(attempt: BroadcastSubmission, error: unknown): BroadcastSubmission | null {
  return error instanceof ApiError && error.status === 409 ? null : attempt
}

export function broadcastFailureMessage(error: Error): string {
  if (!(error instanceof ApiError)) return 'Could not confirm the broadcast result.'
  return error.status === 409
    ? 'This request key was already used for a different broadcast. Review your draft and send it again.'
    : error.message
}

const emailStatusFields = {
  broadcast_id: broadcastIdSchema,
  available: z.boolean(),
  queued: z.number().int().nonnegative(),
  claimed: z.number().int().nonnegative(),
  smtp_accepted: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  cancelled: z.number().int().nonnegative(),
}
const emailStatusSchema = NotificationBroadcastEmailStatusResponseSchema.strip().extend(emailStatusFields).readonly()
const emailCancellationSchema = NotificationBroadcastEmailCancellationResponseSchema.strip().extend({
  ...emailStatusFields,
  cancelled_now: z.number().int().nonnegative(),
}).readonly()

export type BroadcastEmailStatus = z.infer<typeof emailStatusSchema>
export type BroadcastEmailCancellation = z.infer<typeof emailCancellationSchema>

export async function fetchBroadcastEmailStatus(id: BroadcastId, signal?: AbortSignal): Promise<BroadcastEmailStatus> {
  return apiRequest(`/api/notifications/broadcast/${encodeURIComponent(id)}/email-status`, {
    schema: emailStatusSchema.refine((result) => result.broadcast_id === id), signal,
  })
}

export async function cancelBroadcastEmail(id: BroadcastId): Promise<BroadcastEmailCancellation> {
  return apiRequest(`/api/notifications/broadcast/${encodeURIComponent(id)}/cancel-email`, {
    schema: emailCancellationSchema.refine((result) => result.broadcast_id === id), method: 'POST',
  })
}
