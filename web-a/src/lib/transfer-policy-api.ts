import { z } from 'zod'

import { apiRequest } from './http.ts'

export const transferPolicyCurrencySchema = z.enum(['USD', 'EUR', 'GBP', 'VND', 'JPY', 'KWD'], {
  error: 'Choose USD, EUR, GBP, VND, JPY, or KWD.',
})
export const transferPolicyMinorDigits = { USD: 2, EUR: 2, GBP: 2, VND: 0, JPY: 0, KWD: 3 } as const

export const transferPolicySchema = z.object({
  fee_amount_minor: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
  currency: transferPolicyCurrencySchema,
  instructions: z.string().max(4000).refine((value) => value.trim().length > 0),
}).readonly()

export type TransferPolicy = z.infer<typeof transferPolicySchema>

export async function fetchTransferPolicy(signal?: AbortSignal): Promise<TransferPolicy> {
  return apiRequest('/api/admin/transfer-policy', { schema: transferPolicySchema, signal })
}

export async function updateTransferPolicy(policy: TransferPolicy): Promise<TransferPolicy> {
  return apiRequest('/api/admin/transfer-policy', {
    schema: transferPolicySchema,
    method: 'PUT',
    body: policy,
  })
}
