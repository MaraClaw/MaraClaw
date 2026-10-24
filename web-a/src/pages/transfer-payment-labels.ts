import type { ComponentProps } from 'react'

import type { Badge } from '@/components/ui/badge'
import type { TransferSummary } from '@/lib/transfer-reviews-api'

export const transferStatusLabels = {
  payment_review: 'Payment review', admission_created: 'Admission created', rejected: 'Rejected', withdrawn: 'Withdrawn',
} as const satisfies Record<TransferSummary['status'], string>

export const transferStatusVariants = {
  payment_review: 'warning', admission_created: 'success', rejected: 'destructive', withdrawn: 'secondary',
} as const satisfies Record<TransferSummary['status'], ComponentProps<typeof Badge>['variant']>

export const transferActionClass = 'h-auto min-h-11 max-w-full shrink whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function transferFeeLabel(request: TransferSummary): string {
  return `${request.fee_amount_minor.toLocaleString()} minor units · ${request.currency}`
}
