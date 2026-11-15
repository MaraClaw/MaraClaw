import { ChevronRight } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useTranslation } from '@/hooks/use-translation'
import type { ResourcePressureSummary } from '@/lib/resource-pressure-api'
import { summaryRowView } from '@/lib/resource-pressure-format'
import { cn } from '@/lib/utils'

export function UpgradeSignalsCompanySummary({ entry, to }: {
  readonly entry: ResourcePressureSummary
  readonly to?: string
}) {
  const { t, number, date } = useTranslation()
  const row = summaryRowView(entry, { t, number, date })
  const metrics = [
    ['Memory episodes', row.memory], ['CPU episodes', row.cpu],
    ['Workspace disk episodes', row.workspaceDisk], ['Managed storage episodes', row.managedStorage],
    ['OOM kills', row.oomKills], ['Time under pressure', row.pressureTime],
    ['Peak usage', row.maxPeak], ['Agents affected', row.agentsAffected],
  ] as const

  return (
    <Card className={cn('relative min-w-0', to && 'transition-colors hover:bg-muted/40 active:bg-muted/60')}>
      {to ? <Link to={to} aria-label={t('Open {name}', { name: row.company })} className="absolute inset-0 z-10 rounded-[inherit] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2" /> : null}
      <CardHeader className="flex-row items-start justify-between gap-4">
        <CardTitle className="min-w-0 text-lg [overflow-wrap:anywhere]">{row.company}</CardTitle>
        {to ? <ChevronRight className="size-5 shrink-0 text-muted-foreground" aria-hidden /> : null}
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 text-sm sm:grid-cols-3 lg:grid-cols-4">
          <div className="flex min-w-0 flex-col gap-1">
            <dt className="text-muted-foreground">{t('Current tier')}</dt>
            <dd className="font-medium">{row.currentTier}</dd>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <dt className="text-muted-foreground">{t('Suggested tier')}</dt>
            <dd>{row.hasSuggestion ? <Badge variant="warning" className="text-sm font-semibold">{row.suggestedTier}</Badge> : row.suggestedTier}</dd>
          </div>
          {metrics.map(([label, value]) => <div key={label} className="flex min-w-0 flex-col gap-1">
            <dt className="text-muted-foreground">{t(label)}</dt>
            <dd className="font-medium tabular-nums">{value}</dd>
          </div>)}
          <div className="col-span-2 flex min-w-0 flex-col gap-1">
            <dt className="text-muted-foreground">{t('Last seen')}</dt>
            <dd><time dateTime={row.lastSeenAt}>{row.lastSeen}</time></dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  )
}
