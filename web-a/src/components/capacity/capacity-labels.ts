import type { ComponentProps } from 'react'

import type { Badge } from '@/components/ui/badge'
import type { CapacityOrder, CapacityOrderStatus } from '@/lib/capacity-api'

export const capacityStatusLabels = {
  awaiting_payment: 'Awaiting payment', submitted: 'Sent for review', approved: 'Approved',
  rejected: 'Rejected', cancelled: 'Cancelled', expired: 'Expired',
} as const satisfies Record<CapacityOrderStatus, string>

export const capacityStatusVariants = {
  awaiting_payment: 'warning', submitted: 'soft', approved: 'success',
  rejected: 'destructive', cancelled: 'secondary', expired: 'secondary',
} as const satisfies Record<CapacityOrderStatus, ComponentProps<typeof Badge>['variant']>

export const capacityKindLabels = {
  purchase: 'Purchase', renewal: 'Monthly renewal',
} as const satisfies Record<CapacityOrder['kind'], string>

export const capacityTierLabels = { pro: 'Pro', max: 'Max', ultra: 'Ultra' } as const satisfies Record<CapacityOrder['tier'], string>

export const capacityActionClass = 'h-auto min-h-11 max-w-full shrink whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'
