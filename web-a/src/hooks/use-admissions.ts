import { useQuery, useQueryClient } from '@tanstack/react-query'

import type { AdmissionId, AdmissionStatus } from '@/lib/admissions-api'
import { canReviewAdmissions } from '@/lib/types/auth'
import { admissionDecisionOptions, admissionDetailOptions, admissionReviewsOptions } from './admission-options'
import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'

export function useAdmissionReviews(status: AdmissionStatus, offset: number) {
  const { user, mustChangePassword } = useAuth()
  return useQuery(admissionReviewsOptions(status, offset, canReviewAdmissions(user) && !mustChangePassword))
}

export function useAdmissionDetail(id: AdmissionId | null) {
  const { user, mustChangePassword } = useAuth()
  return useQuery(admissionDetailOptions(id, canReviewAdmissions(user) && !mustChangePassword))
}

export function useAdmissionDecision(id: AdmissionId) {
  return useSessionMutation(admissionDecisionOptions(useQueryClient(), id))
}
