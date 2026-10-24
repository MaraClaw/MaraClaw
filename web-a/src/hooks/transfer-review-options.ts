import { queryOptions, skipToken, type QueryClient } from '@tanstack/react-query'

import { ApiError } from '../lib/http.ts'
import {
  decideTransferPayment, fetchReviewTenant, fetchTransferDetail, fetchTransferReviews,
  type TransferDecision, type TransferId, type TransferSummary,
} from '../lib/transfer-reviews-api.ts'
import { notificationKeys } from './notification-options.ts'

export const transferReviewKeys = {
  all: ['admin-transfer-payments'] as const,
  list: ['admin-transfer-payments', 'list'] as const,
  detail: (id: TransferId | null) => ['admin-transfer-payments', 'detail', id] as const,
  tenant: (tenantId: string | null) => ['admin-transfer-review-tenant', tenantId] as const,
}

export function reviewTenantOptions(tenantId: string | null, enabled: boolean) {
  return queryOptions({
    queryKey: transferReviewKeys.tenant(tenantId),
    queryFn: ({ signal }) => fetchReviewTenant(signal),
    enabled: enabled && tenantId !== null,
    retry: false,
  })
}

export function transferReviewsOptions(enabled: boolean) {
  return queryOptions({
    queryKey: transferReviewKeys.list,
    queryFn: ({ signal }) => fetchTransferReviews(signal),
    enabled, refetchInterval: 30_000, retry: false,
  })
}

export function transferDetailOptions(id: TransferId | null, enabled: boolean) {
  return queryOptions({
    queryKey: transferReviewKeys.detail(id),
    queryFn: id ? ({ signal }) => fetchTransferDetail(id, signal) : skipToken,
    enabled: enabled && id !== null, refetchInterval: 30_000, retry: false,
  })
}

export function transferDecisionOptions(client: QueryClient, id: TransferId, allowed: boolean) {
  return {
    mutationFn: (decision: TransferDecision) => {
      if (!allowed) throw new ApiError(403, 'Payment review is not available for this account.')
      return decideTransferPayment(id, decision)
    },
    retry: false,
    scope: { id: `transfer-payment-${id}` },
    onSuccess: (detail: TransferSummary) => {
      void client.cancelQueries({ queryKey: transferReviewKeys.all })
      client.setQueryData(transferReviewKeys.detail(id), detail)
      client.setQueryData<readonly TransferSummary[]>(transferReviewKeys.list, (items) => items?.map((item) => item.id === id ? detail : item))
      void client.invalidateQueries({ queryKey: transferReviewKeys.all })
      void client.invalidateQueries({ queryKey: notificationKeys.all })
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 409) {
        void client.cancelQueries({ queryKey: transferReviewKeys.all })
        void client.invalidateQueries({ queryKey: transferReviewKeys.all })
      }
    },
  }
}
