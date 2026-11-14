import { z } from 'zod'

import {
  CAPACITY_DECISION_NOTE_LIMIT, CAPACITY_EVIDENCE_MAX_BYTES, CAPACITY_SETTING_TEXT_LIMIT, MAX_USD_VND_RATE,
} from './capacity-api.ts'
import { MAX_CAPACITY_EXTRAS } from './capacity-pricing.ts'

function wholeNumber(message: string, within: (count: number) => boolean) {
  return z.string().trim().transform((value, context) => {
    const count = /^\d+$/.test(value) ? Number(value) : Number.NaN
    if (!within(count)) {
      context.addIssue({ code: 'custom', message })
      return z.NEVER
    }
    return count
  })
}

const countField = wholeNumber('Enter a whole number from 0 to 10,000.', (count) => count <= MAX_CAPACITY_EXTRAS)

export const capacityOrderDraftSchema = z.object({ extraAccounts: countField, extraAgents: countField })
  .superRefine((draft, context) => {
    if (draft.extraAccounts === 0 && draft.extraAgents === 0) {
      context.addIssue({ code: 'custom', message: 'Order at least one extra account or agent.', path: ['extraAgents'] })
    }
  })
export type CapacityOrderDraft = z.input<typeof capacityOrderDraftSchema>
export type CapacityOrderValues = z.output<typeof capacityOrderDraftSchema>

export const capacityNoteSchema = z.string().trim()
  .refine((note) => [...note].length <= CAPACITY_DECISION_NOTE_LIMIT, 'Use 1,000 characters or fewer.')
export const capacityDecisionDraftSchema = z.object({ note: capacityNoteSchema })

const settingText = z.string().trim().min(1, 'Enter a value.')
  .refine((value) => [...value].length <= CAPACITY_SETTING_TEXT_LIMIT, 'Use 120 characters or fewer.')
  .refine((value) => !/\p{C}/u.test(value), 'Remove line breaks and other control characters.')
export const capacityPaymentSettingsDraftSchema = z.object({
  bank_name: settingText,
  account_number: settingText,
  branch_name: settingText,
  usd_vnd_rate: wholeNumber('Enter a whole number from 1 to 1,000,000.', (rate) => rate >= 1 && rate <= MAX_USD_VND_RATE),
})
export type CapacityPaymentSettingsDraft = z.input<typeof capacityPaymentSettingsDraftSchema>

export const CAPACITY_EVIDENCE_ACCEPT = 'image/png,image/jpeg,image/webp,application/pdf'
const evidenceTypes: readonly string[] = ['image/png', 'image/jpeg', 'image/webp', 'application/pdf']
const evidenceExtension = /\.(?:png|jpe?g|webp|pdf)$/i

/**
 * Early feedback only: the engine decides the real type from the leading bytes and enforces the same size cap.
 * Returns a translation key, or `null` for a file worth uploading.
 */
export function evidenceFileProblem(file: Pick<File, 'name' | 'type' | 'size'>): string | null {
  const typeKnown = evidenceTypes.includes(file.type)
  const typeUnset = file.type === '' || file.type === 'application/octet-stream'
  if (!typeKnown && !(typeUnset && evidenceExtension.test(file.name))) return 'Choose a PNG, JPEG, WebP or PDF file.'
  if (file.size === 0) return 'This file is empty. Choose another file.'
  if (file.size > CAPACITY_EVIDENCE_MAX_BYTES) return 'This file is larger than 10 MiB. Choose a smaller file.'
  return null
}
