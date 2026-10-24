import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { transferQuoteSchema, transferSummarySchema } from './types/company-transfers.ts'

type RequestContext = { readonly owner: SessionOwner; readonly signal?: AbortSignal }

function requestSignal(signal?: AbortSignal): { readonly signal?: AbortSignal } {
  return { ...(signal === undefined ? {} : { signal }) }
}

export async function getTransferQuote(context: RequestContext) {
  const { owner, signal } = context
  return transferQuoteSchema.parse(await apiRequest<unknown>('/api/company-admissions/transfer-quote', {
    owner, ...requestSignal(signal),
  }))
}

export async function listMyTransfers(context: RequestContext) {
  const { owner, signal } = context
  return z.array(transferSummarySchema).readonly().parse(
    await apiRequest<unknown>('/api/company-admissions/transfers/mine?limit=100', {
      owner, ...requestSignal(signal),
    }),
  )
}

export async function getMyTransfer(id: string, context: RequestContext) {
  const { owner, signal } = context
  return transferSummarySchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/${encodeURIComponent(id)}`, {
    owner, ...requestSignal(signal),
  }))
}

export async function withdrawMyTransfer(id: string, context: RequestContext) {
  const { owner, signal } = context
  return transferSummarySchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/${encodeURIComponent(id)}/withdraw`, {
    owner, ...requestSignal(signal), method: 'POST',
  }))
}
