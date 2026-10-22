import type { AdmissionDetail, AdmissionStatus } from '@/lib/admissions-api'

export const admissionStatusLabels = {
  pending: 'Pending', approved: 'Approved', rejected: 'Rejected', withdrawn: 'Withdrawn',
} as const satisfies Record<AdmissionStatus, string>

export const admissionMethodLabels = {
  invitation: 'Invitation', verified_domain: 'Verified email domain', sso: 'Single sign-on',
} as const satisfies Record<AdmissionDetail['method'], string>
