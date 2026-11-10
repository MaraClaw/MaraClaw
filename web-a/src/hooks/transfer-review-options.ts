import { queryOptions, skipToken, type QueryClient } from '@tanstack/react-query'

import { ApiError } from '../lib/http.ts'
import {
  decideTransferPayment, fetchTransferDetail, fetchTransferReviews,
  type TransferDecision, type TransferId, type TransferReviewPage, type TransferReviewView, type TransferSummary,
} from '../lib/transfer-reviews-api.ts'
import { notificationKeys } from './notification-options.ts'

export const transferReviewKeys = {
  all: ['admin-transfer-payments'] as const,
  lists: ['admin-transfer-payments', 'list'] as const,
  list: (view: TransferReviewView, cursor: string | null) => ['admin-transfer-payments', 'list', view, cursor] as const,
  detail: (id: TransferId | null) => ['admin-transfer-payments', 'detail', id] as const,
}

export function transferReviewsOptions(view: TransferReviewView, cursor: string | null, enabled: boolean) {
  return queryOptions({
    queryKey: transferReviewKeys.list(view, cursor),
    queryFn: ({ signal }) => fetchTransferReviews(view, cursor, signal),
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
      client.setQueriesData<TransferReviewPage>({ queryKey: [...transferReviewKeys.lists, 'pending'] }, (page) =>
        page ? { ...page, items: page.items.filter((item) => item.id !== id) } : page)
      client.setQueriesData<TransferReviewPage>({ queryKey: [...transferReviewKeys.lists, 'recent'] }, (page) =>
        page ? { ...page, items: page.items.map((item) => item.id === id ? detail : item) } : page)
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
