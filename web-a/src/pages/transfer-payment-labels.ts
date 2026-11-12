import type { ComponentProps } from 'react'

import type { Badge } from '@/components/ui/badge'
import type { TransferSummary } from '@/lib/transfer-reviews-api'
import { localeTag, translate, type Locale } from '@/lib/i18n'

export const transferStatusLabels = {
  payment_review: 'Payment review', admission_created: 'Admission created', rejected: 'Rejected', withdrawn: 'Withdrawn',
} as const satisfies Record<TransferSummary['status'], string>

export const transferStatusVariants = {
  payment_review: 'warning', admission_created: 'success', rejected: 'destructive', withdrawn: 'secondary',
} as const satisfies Record<TransferSummary['status'], ComponentProps<typeof Badge>['variant']>

export const transferActionClass = 'h-auto min-h-11 max-w-full shrink whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function transferFeeLabel(request: TransferSummary, locale: Locale = 'en'): string {
  return translate(locale, '{amount} minor units · {currency}', { amount: request.fee_amount_minor.toLocaleString(localeTag[locale]), currency: request.currency })
}
