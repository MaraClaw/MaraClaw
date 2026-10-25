import { z } from 'zod'
import { TransferChoiceSchema, TransferStatusSchema, type TransferChoice } from '../engine-contracts.generated.ts'

const fee = {
  fee_amount_minor: z.number().int().nonnegative(),
  currency: z.string().regex(/^[A-Z]{3}$/),
}
export const transferChoiceSchema = TransferChoiceSchema
export const transferDispositionSchema = z.object({
  transfer_request_id: z.uuid(), choice: transferChoiceSchema, ...fee,
  free_consent_at: z.string().nullable(),
}).readonly()
export const transferQuoteSchema = z.object({
  ...fee, instructions: z.string(), source_eligible: z.boolean(), has_owned_agents: z.boolean(),
  explicit_choice_required: z.boolean(), free_requires_destructive_consent: z.literal(true),
}).readonly()
export const transferSummarySchema = z.object({
  id: z.uuid(), applicant_user_id: z.uuid(), source_tenant_id: z.uuid(), target_tenant_id: z.uuid(),
  choice: transferChoiceSchema,
  status: TransferStatusSchema,
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
  readonly transfer_choice?: TransferChoice
  readonly free_destructive_consent?: boolean
}
