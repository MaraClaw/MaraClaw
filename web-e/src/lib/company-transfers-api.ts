import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { transferPageRequestSchema, transferQuoteSchema, transferSummaryPageSchema, transferSummarySchema, type TransferPageRequest, type TransferSummaryPage } from './types/company-transfers.ts'

type RequestContext = { readonly owner: SessionOwner; readonly signal?: AbortSignal }

function requestSignal(signal?: AbortSignal): { readonly signal?: AbortSignal } {
  return { ...(signal === undefined ? {} : { signal }) }
}

export async function getTransferQuote(context: RequestContext) {
  const { owner, signal } = context
  return apiRequest('/api/company-admissions/transfer-quote', {
    schema: transferQuoteSchema, owner, ...requestSignal(signal),
  })
}

export async function listMyTransfers(context: RequestContext) {
  const { owner, signal } = context
  return apiRequest('/api/company-admissions/transfers/mine?limit=100', {
    schema: z.array(transferSummarySchema).readonly(), owner, ...requestSignal(signal),
  })
}

export async function listMyTransfersPage(page: TransferPageRequest, context: RequestContext): Promise<TransferSummaryPage> {
  const { owner, signal } = context
  const { limit, cursor } = transferPageRequestSchema.parse(page)
  const query = new URLSearchParams({ limit: String(limit) })
  if (cursor !== undefined) query.set('cursor', cursor)
  return apiRequest(`/api/company-admissions/transfers/mine/page?${query}`, {
    schema: transferSummaryPageSchema, owner, ...requestSignal(signal),
  })
}

export async function getMyTransfer(id: string, context: RequestContext) {
  const { owner, signal } = context
  return apiRequest(`/api/company-admissions/transfers/${encodeURIComponent(id)}`, {
    schema: transferSummarySchema, owner, ...requestSignal(signal),
  })
}

export async function withdrawMyTransfer(id: string, context: RequestContext) {
  const { owner, signal } = context
  return apiRequest(`/api/company-admissions/transfers/${encodeURIComponent(id)}/withdraw`, {
    schema: transferSummarySchema, owner, ...requestSignal(signal), method: 'POST',
  })
}
