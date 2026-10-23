import { z } from 'zod'

import { apiRequest } from './http.ts'

export const openClawUpdateDraftSchema = z.object({
  title: z.string().max(200, 'Use at most 200 characters.').refine((value) => value.trim().length > 0, 'Enter a title.'),
  body: z.string().max(1000, 'Use at most 1,000 characters.'),
  mandatory: z.boolean(),
})

export type OpenClawUpdateDraft = Readonly<z.infer<typeof openClawUpdateDraftSchema>>

export const openClawUpdateRequestSchema = openClawUpdateDraftSchema.extend({ request_id: z.uuid() }).readonly()
export type OpenClawUpdateRequest = z.infer<typeof openClawUpdateRequestSchema>

const resultSchema = z.object({
  notice: openClawUpdateDraftSchema.extend({
    id: z.uuid(),
    created_at: z.iso.datetime({ offset: true }),
  }).readonly(),
  recipients: z.number().int().nonnegative(),
}).readonly()

export type OpenClawUpdateResult = z.infer<typeof resultSchema>

export async function sendOpenClawUpdate(payload: OpenClawUpdateRequest): Promise<OpenClawUpdateResult> {
  return resultSchema.parse(await apiRequest<unknown>('/api/admin/openclaw-updates', {
    method: 'POST',
    body: openClawUpdateRequestSchema.parse(payload),
  }))
}
