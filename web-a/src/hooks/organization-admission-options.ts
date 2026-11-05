import { queryOptions, type QueryClient } from '@tanstack/react-query'

import { ApiError } from '../lib/http.ts'
import {
  decideOrganizationAdmission, fetchOrganizationAdmissions, retryOrganizationAdmissionEmail,
  type OrganizationAdmission, type OrganizationAdmissionDecision, type OrganizationAdmissionId,
  type OrganizationAdmissionPage, type OrganizationAdmissionStatus,
} from '../lib/organization-admissions-api.ts'
import { notificationKeys } from './notification-options.ts'

export const organizationAdmissionKeys = {
  all: ['admin-organization-admissions'] as const,
  list: (status: OrganizationAdmissionStatus, offset: number) => ['admin-organization-admissions', status, offset] as const,
}

export function organizationAdmissionsOptions(status: OrganizationAdmissionStatus, offset: number, enabled: boolean) {
  return queryOptions({
    queryKey: organizationAdmissionKeys.list(status, offset),
    queryFn: ({ signal }) => fetchOrganizationAdmissions(status, offset, signal),
    enabled, refetchInterval: 30_000,
  })
}

function replaceOrganizationAdmission(client: QueryClient, detail: OrganizationAdmission) {
  void client.cancelQueries({ queryKey: organizationAdmissionKeys.all })
  client.setQueriesData<OrganizationAdmissionPage>({ queryKey: organizationAdmissionKeys.all }, (page) =>
    page ? { ...page, items: page.items.map((item) => item.id === detail.id ? detail : item) } : page)
  void client.invalidateQueries({ queryKey: organizationAdmissionKeys.all })
}

export function organizationAdmissionDecisionOptions(client: QueryClient, id: OrganizationAdmissionId, allowed: boolean) {
  return {
    mutationFn: (decision: OrganizationAdmissionDecision) => {
      if (!allowed) throw new ApiError(403, 'Organization admission review is unavailable.')
      return decideOrganizationAdmission(id, decision)
    },
    retry: false,
    scope: { id: `organization-admission-decision-${id}` },
    onSuccess: (detail: OrganizationAdmission) => {
      replaceOrganizationAdmission(client, detail)
      void client.invalidateQueries({ queryKey: notificationKeys.all })
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
        void client.cancelQueries({ queryKey: organizationAdmissionKeys.all })
        void client.invalidateQueries({ queryKey: organizationAdmissionKeys.all })
      }
    },
  }
}

export function organizationAdmissionEmailRetryOptions(client: QueryClient, id: OrganizationAdmissionId, allowed: boolean) {
  return {
    mutationFn: () => {
      if (!allowed) throw new ApiError(403, 'Organization admission review is unavailable.')
      return retryOrganizationAdmissionEmail(id)
    },
    retry: false,
    scope: { id: `organization-admission-email-${id}` },
    onSuccess: (detail: OrganizationAdmission) => { replaceOrganizationAdmission(client, detail) },
    onError: (error: Error) => {
      if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
        void client.cancelQueries({ queryKey: organizationAdmissionKeys.all })
        void client.invalidateQueries({ queryKey: organizationAdmissionKeys.all })
      }
    },
  }
}
