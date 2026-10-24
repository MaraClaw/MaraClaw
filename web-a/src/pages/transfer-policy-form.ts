import { z } from 'zod'

import type { TransferPolicy } from '@/lib/transfer-policy-api'

const amountSchema = z.string().trim()
  .regex(/^\d+(?:\.\d{1,2})?$/, 'Enter a nonnegative amount with up to 2 decimal places, such as 12.34.')
  .transform((value) => {
    const [whole, fraction = ''] = value.split('.')
    // Convert integer digits, never a fractional floating-point amount.
    return Number(`${whole}${fraction.padEnd(2, '0')}`)
  })
  .pipe(z.number().int('This amount is too large.').nonnegative())

export const transferPolicyDraftSchema = z.object({
  amount: amountSchema,
  currency: z.string().trim().toUpperCase().regex(/^[A-Z]{3}$/, 'Enter a 3-letter currency code, such as USD.'),
  instructions: z.string().max(4000, 'Use at most 4,000 characters.')
    .refine((value) => value.trim().length > 0, 'Enter payment instructions.'),
}).transform(({ amount, currency, instructions }): TransferPolicy => ({
  fee_amount_minor: amount, currency, instructions,
}))

export type TransferPolicyDraft = Readonly<z.input<typeof transferPolicyDraftSchema>>

export function formatMinorAmount(amount: number): string {
  const digits = String(amount).padStart(3, '0')
  return `${digits.slice(0, -2)}.${digits.slice(-2)}`
}

export function transferPolicyToDraft(policy: TransferPolicy): TransferPolicyDraft {
  return {
    amount: formatMinorAmount(policy.fee_amount_minor),
    currency: policy.currency,
    instructions: policy.instructions,
  }
}
