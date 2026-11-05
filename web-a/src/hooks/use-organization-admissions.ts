import { useQuery, useQueryClient } from '@tanstack/react-query'

import type { OrganizationAdmissionId, OrganizationAdmissionStatus } from '@/lib/organization-admissions-api'
import { canReviewOrganizationAdmissions } from '@/lib/types/auth'
import { organizationAdmissionDecisionOptions, organizationAdmissionEmailRetryOptions, organizationAdmissionsOptions } from './organization-admission-options'
import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'

export function useOrganizationAdmissions(status: OrganizationAdmissionStatus, offset: number) {
  const { user, mustChangePassword } = useAuth()
  return useQuery(organizationAdmissionsOptions(status, offset, canReviewOrganizationAdmissions(user) && !mustChangePassword))
}

export function useOrganizationAdmissionDecision(id: OrganizationAdmissionId) {
  const { user, mustChangePassword } = useAuth()
  return useSessionMutation(organizationAdmissionDecisionOptions(useQueryClient(), id, canReviewOrganizationAdmissions(user) && !mustChangePassword))
}

export function useOrganizationAdmissionEmailRetry(id: OrganizationAdmissionId) {
  const { user, mustChangePassword } = useAuth()
  return useSessionMutation(organizationAdmissionEmailRetryOptions(useQueryClient(), id, canReviewOrganizationAdmissions(user) && !mustChangePassword))
}
