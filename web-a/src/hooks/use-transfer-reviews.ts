import { useQuery, useQueryClient } from '@tanstack/react-query'

import { canInspectReviewTenant, canReviewTransferPayments } from '@/lib/transfer-review-access'
import type { TransferId } from '@/lib/transfer-reviews-api'
import { reviewTenantOptions, transferDecisionOptions, transferDetailOptions, transferReviewsOptions } from './transfer-review-options'
import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'

export function useTransferReviewAccess() {
  const { user, mustChangePassword } = useAuth()
  const inspectTenant = canInspectReviewTenant(user) && !mustChangePassword
  const tenant = useQuery(reviewTenantOptions(user?.tenant_id ?? null, inspectTenant))
  return {
    canReview: !mustChangePassword && (!inspectTenant || !tenant.isError) && canReviewTransferPayments(user, tenant.data),
    isPending: inspectTenant && tenant.isPending,
    error: inspectTenant ? tenant.error : null,
    isFetching: inspectTenant && tenant.isFetching,
    retry: tenant.refetch,
  }
}

export function useTransferReviews() {
  return useQuery(transferReviewsOptions(useTransferReviewAccess().canReview))
}

export function useTransferDetail(id: TransferId | null) {
  return useQuery(transferDetailOptions(id, useTransferReviewAccess().canReview))
}

export function useTransferDecision(id: TransferId) {
  const { canReview } = useTransferReviewAccess()
  return useSessionMutation(transferDecisionOptions(useQueryClient(), id, canReview))
}
