import type { AdmissionSummary } from '@/lib/types/company-admissions'

export const admissionStatuses = {
  pending: { label: 'Awaiting review', description: 'Your request is awaiting company review. Your current organization has not changed.' },
  rejected: { label: 'Rejected', description: 'The company declined your request. Your current organization has not changed.' },
  approved: { label: 'Approved', description: 'The company approved your request. Open its workspace to refresh your session.' },
} satisfies Record<AdmissionSummary['status'], { readonly label: string; readonly description: string }>
