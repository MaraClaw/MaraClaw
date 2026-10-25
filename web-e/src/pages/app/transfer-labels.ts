import type { TransferSummary } from '@/lib/types/company-transfers'

export const transferStatuses = {
  payment_review: { label: 'Awaiting payment review', description: 'Payment review is in progress. No admission request exists yet. Your current organization, agents and integrations have not changed.' },
  admission_created: { label: 'Destination admission created', description: 'New invitation transfers join automatically after payment approval. Open the saved admission for its recorded status; older requests may still be pending.' },
  rejected: { label: 'Payment review rejected', description: 'This transfer did not proceed. Your organization, agents and integrations have not changed.' },
  withdrawn: { label: 'Transfer withdrawn', description: 'This transfer did not proceed. Your organization, agents and integrations have not changed.' },
} satisfies Record<TransferSummary['status'], { readonly label: string; readonly description: string }>
