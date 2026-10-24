import { z } from 'zod'

import { apiRequest } from './http.ts'

export const transferPolicySchema = z.object({
  fee_amount_minor: z.number().int().nonnegative(),
  currency: z.string().regex(/^[A-Z]{3}$/),
  instructions: z.string().max(4000).refine((value) => value.trim().length > 0),
}).readonly()

export type TransferPolicy = z.infer<typeof transferPolicySchema>

export async function fetchTransferPolicy(signal?: AbortSignal): Promise<TransferPolicy> {
  return transferPolicySchema.parse(await apiRequest<unknown>('/api/admin/transfer-policy', { signal }))
}

export async function updateTransferPolicy(policy: TransferPolicy): Promise<TransferPolicy> {
  return transferPolicySchema.parse(await apiRequest<unknown>('/api/admin/transfer-policy', {
    method: 'PUT',
    body: policy,
  }))
}
