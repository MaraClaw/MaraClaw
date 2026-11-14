import { Badge } from '@/components/ui/badge'
import { useTranslation } from '@/hooks/use-translation'
import type { CapacityOrderStatus } from '@/lib/capacity-api'
import { capacityStatusLabels, capacityStatusVariants } from './capacity-labels'

export function CapacityStatusBadge({ status }: { readonly status: CapacityOrderStatus }) {
  const { t } = useTranslation()
  return <Badge variant={capacityStatusVariants[status]}>{t(capacityStatusLabels[status])}</Badge>
}
