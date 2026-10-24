import { z } from 'zod'

import { transferPolicyCurrencySchema, transferPolicyMinorDigits, type TransferPolicy } from '../lib/transfer-policy-api.ts'

export const transferPolicyDraftSchema = z.object({
  amount: z.string().trim().regex(/^\d+(?:\.\d+)?$/, 'Enter a nonnegative decimal amount.'),
  currency: z.string().trim().toUpperCase().pipe(transferPolicyCurrencySchema),
  instructions: z.string().max(4000, 'Use at most 4,000 characters.')
    .refine((value) => value.trim().length > 0, 'Enter payment instructions.'),
}).transform(({ amount, currency, instructions }, context): TransferPolicy | typeof z.NEVER => {
  const places = transferPolicyMinorDigits[currency]
  const [whole, fraction = ''] = amount.split('.')
  if (fraction.length > places) {
    context.addIssue({ code: 'custom', path: ['amount'], message: places === 0
      ? `${currency} requires a whole-number amount; decimal places are not allowed.`
      : `${currency} allows up to ${places} decimal places.` })
    return z.NEVER
  }
  const minor = BigInt(`${whole}${fraction.padEnd(places, '0')}`)
  if (minor > BigInt(Number.MAX_SAFE_INTEGER)) {
    context.addIssue({ code: 'custom', path: ['amount'], message: 'This amount is too large.' })
    return z.NEVER
  }
  return { fee_amount_minor: Number(minor), currency, instructions }
})

export type TransferPolicyDraft = Readonly<z.input<typeof transferPolicyDraftSchema>>

export function formatMinorAmount(amount: number, currency: TransferPolicy['currency']): string {
  const places = transferPolicyMinorDigits[currency]
  if (places === 0) return String(amount)
  const digits = String(amount).padStart(places + 1, '0')
  return `${digits.slice(0, -places)}.${digits.slice(-places)}`
}

export function transferPolicyToDraft(policy: TransferPolicy): TransferPolicyDraft {
  return {
    amount: formatMinorAmount(policy.fee_amount_minor, policy.currency),
    currency: policy.currency,
    instructions: policy.instructions,
  }
}
