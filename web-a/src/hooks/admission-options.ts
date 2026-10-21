import { queryOptions, skipToken, type QueryClient } from '@tanstack/react-query'

import {
  decideAdmission, fetchAdmissionDetail, fetchAdmissionReviews,
  type AdmissionDecision, type AdmissionDetail, type AdmissionId, type AdmissionStatus,
} from '../lib/admissions-api.ts'
import { ApiError } from '../lib/http.ts'
import { notificationKeys } from './notification-options.ts'

export const admissionKeys = {
  all: ['admin-admission-reviews'] as const,
  lists: ['admin-admission-reviews', 'list'] as const,
  list: (status: AdmissionStatus, offset: number) => ['admin-admission-reviews', 'list', status, offset] as const,
  detail: (id: AdmissionId | null) => ['admin-admission-reviews', 'detail', id] as const,
}

export function admissionReviewsOptions(status: AdmissionStatus, offset: number, enabled: boolean) {
  return queryOptions({
    queryKey: admissionKeys.list(status, offset),
    queryFn: ({ signal }) => fetchAdmissionReviews(status, offset, signal),
    enabled,
    refetchInterval: 30_000,
  })
}

export function admissionDetailOptions(id: AdmissionId | null, enabled: boolean) {
  return queryOptions({
    queryKey: admissionKeys.detail(id),
    queryFn: id ? ({ signal }) => fetchAdmissionDetail(id, signal) : skipToken,
    enabled: enabled && id !== null,
    refetchInterval: 30_000,
  })
}

export function admissionDecisionOptions(client: QueryClient, id: AdmissionId) {
  return {
    mutationFn: (decision: AdmissionDecision) => decideAdmission(id, decision),
    retry: false,
    scope: { id: `admission-decision-${id}` },
    onSuccess: (detail: AdmissionDetail) => {
      void client.cancelQueries({ queryKey: admissionKeys.all })
      client.setQueryData(admissionKeys.detail(id), detail)
      client.setQueriesData<readonly AdmissionDetail[]>({ queryKey: [...admissionKeys.lists, 'pending'] }, (items) =>
        items?.filter((item) => item.id !== id))
      void client.invalidateQueries({ queryKey: admissionKeys.all })
      void client.invalidateQueries({ queryKey: notificationKeys.all })
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 409) {
        void client.cancelQueries({ queryKey: admissionKeys.all })
        void client.invalidateQueries({ queryKey: admissionKeys.all })
      }
    },
  }
}
