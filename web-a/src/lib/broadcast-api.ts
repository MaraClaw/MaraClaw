import { z } from 'zod'

import { apiRequest } from './http.ts'

export const broadcastDraftSchema = z.object({
  title: z.string().max(200, 'Use at most 200 characters.').refine((value) => value.trim().length > 0, 'Enter a title.'),
  body: z.string().max(1000, 'Use at most 1,000 characters.'),
  send_email: z.boolean(),
})

export type BroadcastDraft = Readonly<z.infer<typeof broadcastDraftSchema>>

export const broadcastIdSchema = z.uuid().brand<'BroadcastId'>()
export type BroadcastId = z.infer<typeof broadcastIdSchema>

const broadcastResultSchema = z.object({
  ok: z.literal(true),
  broadcast_id: broadcastIdSchema,
  users_notified: z.number().int().nonnegative(),
  agents_notified: z.number().int().nonnegative(),
  emails_sent: z.number().int().nonnegative(),
}).readonly()

export type BroadcastResult = z.infer<typeof broadcastResultSchema>

export async function sendBroadcast(payload: BroadcastDraft): Promise<BroadcastResult> {
  return broadcastResultSchema.parse(await apiRequest<unknown>('/api/notifications/broadcast', {
    method: 'POST',
    body: payload,
  }))
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
const emailStatusSchema = z.object(emailStatusFields).readonly()
const emailCancellationSchema = z.object({
  ...emailStatusFields,
  cancelled_now: z.number().int().nonnegative(),
}).readonly()

export type BroadcastEmailStatus = z.infer<typeof emailStatusSchema>
export type BroadcastEmailCancellation = z.infer<typeof emailCancellationSchema>

export async function fetchBroadcastEmailStatus(id: BroadcastId, signal?: AbortSignal): Promise<BroadcastEmailStatus> {
  return emailStatusSchema.refine((result) => result.broadcast_id === id).parse(
    await apiRequest<unknown>(`/api/notifications/broadcast/${encodeURIComponent(id)}/email-status`, { signal }),
  )
}

export async function cancelBroadcastEmail(id: BroadcastId): Promise<BroadcastEmailCancellation> {
  return emailCancellationSchema.refine((result) => result.broadcast_id === id).parse(
    await apiRequest<unknown>(`/api/notifications/broadcast/${encodeURIComponent(id)}/cancel-email`, { method: 'POST' }),
  )
}
