import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { transferQuoteSchema, transferSummarySchema } from './types/company-transfers.ts'

type RequestContext = { readonly owner: SessionOwner; readonly signal?: AbortSignal }

export async function getTransferQuote(context: RequestContext) {
  return transferQuoteSchema.parse(await apiRequest<unknown>('/api/company-admissions/transfer-quote', context))
}

export async function listMyTransfers(context: RequestContext) {
  return z.array(transferSummarySchema).readonly().parse(
    await apiRequest<unknown>('/api/company-admissions/transfers/mine?limit=100', context),
  )
}

export async function getMyTransfer(id: string, context: RequestContext) {
  return transferSummarySchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/${encodeURIComponent(id)}`, context))
}

export async function withdrawMyTransfer(id: string, context: RequestContext) {
  return transferSummarySchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/${encodeURIComponent(id)}/withdraw`, {
    ...context, method: 'POST',
  }))
}
