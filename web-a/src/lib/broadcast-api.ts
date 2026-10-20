import { z } from 'zod'

import { apiRequest } from './http.ts'

export const broadcastDraftSchema = z.object({
  title: z.string().max(200, 'Use at most 200 characters.').refine((value) => value.trim().length > 0, 'Enter a title.'),
  body: z.string().max(1000, 'Use at most 1,000 characters.'),
  send_email: z.boolean(),
})

export type BroadcastDraft = Readonly<z.infer<typeof broadcastDraftSchema>>

const broadcastResultSchema = z.object({
  ok: z.literal(true),
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
