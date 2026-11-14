import { capacityTierLabels } from '@/components/capacity/capacity-labels'
import { Badge } from '@/components/ui/badge'
import type { CompanyStats } from '@/lib/companies-api'

export function CompanyPlanBadge({ tier }: { readonly tier: CompanyStats['tier'] }) {
  return <Badge variant="outline">{capacityTierLabels[tier]}</Badge>
}
