// Chart-free on purpose: the overview route imports this module, so recharts stays in the analytics tab's chunk.
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError } from '@/lib/http'
import { getSearchAnalyticsOrgs, getSearchAnalyticsSummary } from '@/lib/search-analytics-api'
import { useTranslation } from '@/hooks/use-translation'

export type RangeKey = '7' | '30' | '90'

export function rangeFor(key: RangeKey): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end.getTime() - Number(key) * 24 * 60 * 60 * 1000)
  return { start: start.toISOString(), end: end.toISOString() }
}

export function pct(part: number, total: number): number {
  if (!total) return 0
  return Math.round((part / total) * 1000) / 10
}

export function useAnalyticsFormatting() {
  const { number, date } = useTranslation()
  return {
    formatCount: (value: number | undefined) => value == null ? '—' : number(value),
    shortDate: (value: string) => date(`${value}T00:00:00Z`, { month: 'short', day: 'numeric', timeZone: 'UTC' }),
    rateLabel: (part: number, total: number) => number(pct(part, total) / 100, { style: 'percent', maximumFractionDigits: 1 }),
  }
}

export function SearchAnalyticsSnapshot() {
  const { t } = useTranslation()
  const { formatCount, rateLabel } = useAnalyticsFormatting()
  const window = useMemo(() => rangeFor('7'), [])
  const summary = useQuery({
    queryKey: ['admin-search-analytics-summary', window, 'system'],
    queryFn: ({ signal }) => getSearchAnalyticsSummary(window, signal),
  })
  const orgs = useQuery({
    queryKey: ['admin-search-analytics-orgs', window],
    queryFn: ({ signal }) => getSearchAnalyticsOrgs(window, signal),
  })
  const data = summary.data
  const top = (orgs.data ?? []).slice(0, 3)

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>{t('Search analytics')}</CardTitle>
          <CardDescription>{t('Last 7 days across the entire system.')}</CardDescription>
        </div>
        <Button variant="ghost" size="sm" asChild>
          <Link to="/search-engine?tab=analytics">{t('Open dashboard')}</Link>
        </Button>
      </CardHeader>
      <CardContent className="grid gap-4">
        {orgs.isError ? <ErrorToast error={orgs.error} message={t('Could not load organization search analytics.')} /> : null}
        {summary.isLoading || orgs.isLoading ? (
          <p className="text-sm text-muted-foreground">{t('Loading last 7 days…')}</p>
        ) : summary.error ? (
          <ErrorToast error={summary.error} message={summary.error instanceof ApiError ? summary.error.message : t('Failed to load search analytics')} />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-4">
              <MiniStat label={t('Searches')} value={formatCount(data?.event_count)} />
              <MiniStat label={t('Error rate')} value={data ? rateLabel(data.error_count, data.event_count) : '—'} />
              <MiniStat
                label={t('Quota pressure')}
                value={data ? rateLabel(data.quota_count, data.event_count) : '—'}
              />
              <MiniStat label={t('Companies')} value={formatCount(data?.unique_orgs)} />
            </div>
            {(data?.event_count ?? 0) === 0 ? (
              <p className="text-sm text-muted-foreground">{t('No search requests yet this week.')}</p>
            ) : top.length > 0 ? (
              <p className="text-sm text-muted-foreground">
                {t('Busiest: {names}.', { names: top.map((org) => org.name).join(', ') })}
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">
                {t('{count} searches, none attributed to a company.', { count: formatCount(data?.event_count) })}
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="font-display text-lg font-semibold tabular-nums">{value}</p>
    </div>
  )
}
