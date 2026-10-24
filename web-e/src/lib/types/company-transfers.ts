import { z } from 'zod'

const fee = {
  fee_amount_minor: z.number().int().nonnegative(),
  currency: z.string().regex(/^[A-Z]{3}$/),
}
export const transferChoiceSchema = z.enum(['paid', 'free'])
export const transferDispositionSchema = z.object({
  transfer_request_id: z.uuid(), choice: transferChoiceSchema, ...fee,
  free_consent_at: z.string().nullable(),
}).readonly()
export const transferQuoteSchema = z.object({
  ...fee, instructions: z.string(), source_eligible: z.boolean(), has_owned_agents: z.boolean(),
  explicit_choice_required: z.boolean(), free_requires_destructive_consent: z.boolean(),
}).readonly()
export const transferSummarySchema = z.object({
  id: z.uuid(), applicant_user_id: z.uuid(), source_tenant_id: z.uuid(), target_tenant_id: z.uuid(),
  choice: transferChoiceSchema,
  status: z.enum(['payment_review', 'admission_created', 'rejected', 'withdrawn']),
  ...fee, instructions: z.string(), created_at: z.string(),
  free_consent_at: z.string().nullable(), payment_confirmed_at: z.string().nullable(),
  reviewed_at: z.string().nullable(), reviewed_by_user_id: z.uuid().nullable(),
  admission_request_id: z.uuid().nullable(), review_reason: z.string().nullable(),
}).readonly()
export const paymentReviewResponseSchema = z.object({
  status: z.literal('payment_review'), transfer_request: transferSummarySchema,
}).readonly()
export type TransferQuote = z.infer<typeof transferQuoteSchema>
export type TransferSummary = z.infer<typeof transferSummarySchema>
export type TransferSelection = Readonly<
  { transfer_choice: 'paid' } | { transfer_choice: 'free'; free_destructive_consent: true }
>
export type TransferInput = {
  readonly password: string
  readonly invitation_code?: string
  readonly tenant_id?: string
  readonly transfer_choice?: 'paid' | 'free'
  readonly free_destructive_consent?: boolean
}
