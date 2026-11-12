import { useQuery } from '@tanstack/react-query'
import { useReducedMotion } from 'framer-motion'
import { useId, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { companyOptions } from '@/hooks/company-options'
import { ApiError } from '@/lib/http'
import {
  getSearchAnalyticsDashboard,
  getSearchAnalyticsExportStatus,
  getSearchAnalyticsOrgs,
  getSearchAnalyticsSummary,
  type SearchAnalyticsOrg,
  type SearchAnalyticsPoint,
  type SearchAnalyticsSummary,
} from '@/lib/search-analytics-api'
import { cn } from '@/lib/utils'
import { useTranslation } from '@/hooks/use-translation'

type RangeKey = '7' | '30' | '90'

const KIND_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
]

function rangeFor(key: RangeKey): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end.getTime() - Number(key) * 24 * 60 * 60 * 1000)
  return { start: start.toISOString(), end: end.toISOString() }
}

function asRange(value: string | null): RangeKey {
  if (value === '30' || value === '90') return value
  return '7'
}

function pct(part: number, total: number): number {
  if (!total) return 0
  return Math.round((part / total) * 1000) / 10
}

function useAnalyticsFormatting() {
  const { number, date } = useTranslation()
  return {
    formatCount: (value: number | undefined) => value == null ? '—' : number(value),
    shortDate: (value: string) => date(`${value}T00:00:00Z`, { month: 'short', day: 'numeric', timeZone: 'UTC' }),
    rateLabel: (part: number, total: number) => number(pct(part, total) / 100, { style: 'percent', maximumFractionDigits: 1 }),
  }
}

function padSeries(points: SearchAnalyticsPoint[], startIso: string, endIso: string): SearchAnalyticsPoint[] {
  const byDate = new Map(points.map((point) => [point.date.slice(0, 10), point]))
  const start = new Date(startIso)
  const end = new Date(endIso)
  const cursor = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate()))
  const last = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()))
  const filled: SearchAnalyticsPoint[] = []
  while (cursor < last) {
    const key = cursor.toISOString().slice(0, 10)
    filled.push(byDate.get(key) ?? { date: key, event_count: 0, error_count: 0, quota_count: 0 })
    cursor.setUTCDate(cursor.getUTCDate() + 1)
  }
  return filled
}

export function SearchEngineAnalytics() {
  const { t, number } = useTranslation()
  const filterId = useId()
  const { formatCount, shortDate, rateLabel } = useAnalyticsFormatting()
  const [params, setParams] = useSearchParams()
  const range = asRange(params.get('range'))
  const tenantId = params.get('company') ?? ''
  const reduceMotion = useReducedMotion()
  const window = useMemo(() => rangeFor(range), [range])
  const scoped = { ...window, tenantId: tenantId || undefined }

  const companies = useQuery(companyOptions())
  const dashboard = useQuery({
    queryKey: ['admin-search-analytics-dashboard', scoped],
    queryFn: ({ signal }) => getSearchAnalyticsDashboard(scoped, signal),
  })
  const exportStatus = useQuery({
    queryKey: ['admin-search-analytics-export'],
    queryFn: ({ signal }) => getSearchAnalyticsExportStatus(signal),
  })

  const error = dashboard.error
    ? dashboard.error instanceof ApiError ? dashboard.error.message : t('Failed to load search analytics')
    : null

  const systemWide = !tenantId
  const selectedCompany = (companies.data ?? []).find((company) => company.id === tenantId)
  const unknownCompany = Boolean(tenantId && !selectedCompany && !companies.isLoading)
  const totals = dashboard.data?.selected_summary
  const platform = dashboard.data?.system_summary
  const kindMix = totals?.by_kind ?? []
  const chartData = dashboard.data
    ? padSeries(dashboard.data.timeseries, window.start, window.end).map((point) => ({
        ...point,
        label: shortDate(point.date),
      }))
    : []
  const insights = useInsights({
    systemWide,
    companyName: selectedCompany?.name ?? t('This company'),
    scoped: totals,
    platform,
    orgs: dashboard.data?.orgs ?? [],
    trendingTop: dashboard.data?.trending[0],
  })

  function update(next: { range?: RangeKey; company?: string }) {
    const copy = new URLSearchParams(params)
    copy.set('tab', 'analytics')
    copy.set('range', next.range ?? range)
    const company = next.company === undefined ? tenantId : next.company
    if (company) copy.set('company', company)
    else copy.delete('company')
    setParams(copy, { replace: true })
  }

  return (
    <div className="grid gap-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-display text-xl font-semibold tracking-tight">
              {systemWide ? t('Entire system') : selectedCompany?.name || t('Unknown company')}
            </h2>
            <Badge variant={systemWide ? 'secondary' : 'outline'}>
              {t(systemWide ? 'All companies' : 'Per organization')}
            </Badge>
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {t(systemWide
              ? 'Platform search volume, quota pressure, and queries that appear across companies.'
              : 'This company’s share of platform search, errors, and what its agents look up.')}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label htmlFor={`${filterId}-range`} className="grid gap-1 text-sm">
            <span className="text-muted-foreground">{t('Range')}</span>
            <Select
              id={`${filterId}-range`}
              fit
              value={range}
              onChange={(event) => update({ range: asRange(event.target.value) })}
            >
              <option value="7">{t('Last 7 days')}</option>
              <option value="30">{t('Last 30 days')}</option>
              <option value="90">{t('Last 90 days')}</option>
            </Select>
          </label>
          <label htmlFor={`${filterId}-scope`} className="grid gap-1 text-sm">
            <span className="text-muted-foreground">{t('Scope')}</span>
            <Select
              id={`${filterId}-scope`}
              fit
              value={tenantId}
              onChange={(event) => update({ company: event.target.value })}
              aria-label={t('Analytics scope')}
            >
              <option value="">{t('Entire system')}</option>
              {unknownCompany ? <option value={tenantId}>{t('Unknown company')}</option> : null}
              {(companies.data ?? []).map((company) => (
                <option key={company.id} value={company.id}>
                  {company.name}
                </option>
              ))}
            </Select>
          </label>
        </div>
      </div>

      {error ? <ErrorToast error={dashboard.error} message={error} /> : null}
      {companies.isError ? <ErrorToast error={companies.error} message={t('Could not load companies for search analytics.')} /> : null}
      {exportStatus.isError ? <ErrorToast error={exportStatus.error} message={t('Could not load search export status.')} /> : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label={t('Searches')}
          value={formatCount(totals?.event_count)}
          hint={
            !systemWide && platform?.event_count
              ? t('{share} of the platform', { share: rateLabel(totals?.event_count ?? 0, platform.event_count) })
              : t('{count} agents', { count: formatCount(totals?.unique_agents) })
          }
          loading={dashboard.isLoading}
        />
        <KpiCard
          label={t('Error rate')}
          value={totals ? `${rateLabel(totals.error_count, totals.event_count)}` : '—'}
          hint={t('{count} failed calls', { count: formatCount(totals?.error_count) })}
          tone={(totals?.event_count ?? 0) > 0 && pct(totals?.error_count ?? 0, totals?.event_count ?? 0) >= 10 ? 'warn' : 'default'}
          loading={dashboard.isLoading}
        />
        <KpiCard
          label={t('Quota pressure')}
          value={totals ? `${rateLabel(totals.quota_count, totals.event_count)}` : '—'}
          hint={t('{count} quota responses', { count: formatCount(totals?.quota_count) })}
          tone={(totals?.event_count ?? 0) > 0 && pct(totals?.quota_count ?? 0, totals?.event_count ?? 0) >= 5 ? 'warn' : 'default'}
          loading={dashboard.isLoading}
        />
        <KpiCard
          label={t(systemWide ? 'Companies searching' : 'Avg latency')}
          value={
            systemWide
              ? formatCount(totals?.unique_orgs)
              : totals?.avg_latency_ms == null
                ? '—'
                : t('{count} ms', { count: number(Math.round(totals.avg_latency_ms)) })
          }
          hint={
            systemWide
              ? t('{count} unattributed', { count: formatCount(totals?.unattributed_count) })
              : t('{count} agents in this org', { count: formatCount(totals?.unique_agents) })
          }
          loading={dashboard.isLoading}
        />
      </div>

      {insights.length > 0 ? (
        <ul className="grid gap-2 rounded-2xl border border-border/80 bg-card px-4 py-3 text-sm">
          {insights.map((item) => (
            <li key={item} className="text-muted-foreground">
              {item}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>{t('Daily volume')}</CardTitle>
            <CardDescription>
              {t(systemWide ? 'Tavily calls across every company.' : 'Tavily calls for this company.')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <VolumeChart data={chartData} loading={dashboard.isLoading} reduceMotion={Boolean(reduceMotion)} />
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>{t('Kind mix')}</CardTitle>
            <CardDescription>{t('Tavily search requests.')}</CardDescription>
          </CardHeader>
          <CardContent>
            <KindMix items={kindMix} total={totals?.event_count ?? 0} loading={dashboard.isLoading} />
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {systemWide ? (
          <Card>
            <CardHeader>
              <CardTitle>{t('Companies')}</CardTitle>
              <CardDescription>{t('Open a row to see that organization’s dashboard.')}</CardDescription>
            </CardHeader>
            <CardContent>
              <CompanyList
                rows={dashboard.data?.orgs ?? []}
                loading={dashboard.isLoading}
                onOpen={(id) => update({ company: id })}
              />
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <CardTitle>{t('Share of platform')}</CardTitle>
              <CardDescription>{t('How this company compares to the entire system in the same range.')}</CardDescription>
            </CardHeader>
            <CardContent>
              <ShareOfPlatform scoped={totals} platform={platform} loading={dashboard.isLoading} />
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>{t(systemWide ? 'Queries across companies' : 'What this org searches')}</CardTitle>
            <CardDescription>
              {t(systemWide ? 'Normalized wording only. Ranked by how many companies share them.' : 'Normalized wording only. Ranked by hits.')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <TrendList
              rows={dashboard.data?.trending ?? []}
              loading={dashboard.isLoading}
              showOrgs={systemWide}
            />
          </CardContent>
        </Card>
      </div>

      {exportStatus.data ? (
        <p className="text-xs text-muted-foreground">
          {t('Capture {capture}. Warehouse export {export}. {count} pending.', { capture: t(exportStatus.data.capture_enabled ? 'on' : 'off'), export: t(exportStatus.data.export_enabled ? 'on' : 'off'), count: number(exportStatus.data.pending) })}
          {exportStatus.data.bucket ? ` → ${exportStatus.data.prefix}` : ''}
        </p>
      ) : null}
    </div>
  )
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

function useInsights({
  systemWide,
  companyName,
  scoped,
  platform,
  orgs,
  trendingTop,
}: {
  systemWide: boolean
  companyName: string
  scoped?: SearchAnalyticsSummary
  platform?: SearchAnalyticsSummary
  orgs: SearchAnalyticsOrg[]
  trendingTop?: { query_normalized: string; distinct_orgs: number; hits: number }
}): string[] {
  const { t, number } = useTranslation()
  const { rateLabel } = useAnalyticsFormatting()
  if (!scoped || scoped.event_count === 0) return []
  const lines: string[] = []
  const quotaRate = pct(scoped.quota_count, scoped.event_count)
  const errorRate = pct(scoped.error_count, scoped.event_count)
  const topKind = [...scoped.by_kind].sort((a, b) => b.event_count - a.event_count)[0]
  if (quotaRate >= 5) {
    lines.push(
      systemWide
        ? t('Quota responses are {rate} of volume. Companies at the top of the list are the upsell candidates.', { rate: rateLabel(scoped.quota_count, scoped.event_count) })
        : t('{company} is hitting quota on {rate} of calls.', { company: companyName, rate: rateLabel(scoped.quota_count, scoped.event_count) }),
    )
  }
  if (errorRate >= 10) {
    lines.push(t('Error rate is {rate}. Check upstream failures and exhausted keys.', { rate: rateLabel(scoped.error_count, scoped.event_count) }))
  }
  if (topKind && pct(topKind.event_count, scoped.event_count) >= 50) {
    lines.push(t('Most calls are {kind} ({rate}).', { kind: topKind.kind, rate: rateLabel(topKind.event_count, scoped.event_count) }))
  }
  if (systemWide && orgs[0]) {
    lines.push(
      t('{company} leads volume with {count} searches and {quota} quota hits.', { company: orgs[0].name, count: number(orgs[0].event_count), quota: number(orgs[0].quota_count) }),
    )
  }
  if (!systemWide && platform?.event_count) {
    lines.push(t('{company} accounts for {rate} of platform searches.', { company: companyName, rate: rateLabel(scoped.event_count, platform.event_count) }))
  }
  if (trendingTop?.query_normalized) {
    lines.push(
      systemWide && trendingTop.distinct_orgs > 1
        ? t('“{query}” shows up at {count} companies.', { query: trendingTop.query_normalized, count: number(trendingTop.distinct_orgs) })
        : t('Top query: “{query}” ({count} hits).', { query: trendingTop.query_normalized, count: number(trendingTop.hits) }),
    )
  }
  return lines.slice(0, 3)
}

function KpiCard({
  label,
  value,
  hint,
  tone = 'default',
  loading,
}: {
  label: string
  value: string
  hint: string
  tone?: 'default' | 'warn'
  loading?: boolean
}) {
  const { t } = useTranslation()
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className={cn('text-2xl tabular-nums', tone === 'warn' && 'text-destructive')}>
          {loading ? '…' : value}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-xs text-muted-foreground">{loading ? t('Loading') : hint}</CardContent>
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

function VolumeChart({
  data,
  loading,
  reduceMotion,
}: {
  data: (SearchAnalyticsPoint & { label: string })[]
  loading: boolean
  reduceMotion: boolean
}) {
  const { t, number } = useTranslation()
  if (loading) return <p className="text-sm text-muted-foreground">{t('Loading chart…')}</p>
  if (data.length === 0) return <p className="text-sm text-muted-foreground">{t('No search events in this range.')}</p>
  return (
    <div className="h-56 w-full" role="img" aria-label={t('Daily search volume')}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 12, fill: 'var(--muted-foreground)' }} axisLine={false} tickLine={false} />
          <YAxis allowDecimals={false} tickFormatter={(value: number) => number(value)} tick={{ fontSize: 12, fill: 'var(--muted-foreground)' }} axisLine={false} tickLine={false} width={36} />
          <Tooltip
            contentStyle={{
              background: 'var(--card)',
              border: '1px solid var(--border)',
              borderRadius: 12,
              fontSize: 12,
            }}
            formatter={(value, name) => [number(Number(value)), String(name)]}
          />
          <Area
            type="monotone"
            dataKey="event_count"
            name={t('Searches')}
            stroke="var(--chart-1)"
            fill="var(--chart-1)"
            fillOpacity={0.18}
            strokeWidth={2}
            isAnimationActive={!reduceMotion}
          />
          <Area
            type="monotone"
            dataKey="quota_count"
            name={t('Quota')}
            stroke="var(--chart-5)"
            fill="var(--chart-5)"
            fillOpacity={0.12}
            strokeWidth={1.5}
            isAnimationActive={!reduceMotion}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function KindMix({
  items,
  total,
  loading,
}: {
  items: { kind: string; event_count: number }[]
  total: number
  loading: boolean
}) {
  const { t, number } = useTranslation()
  if (loading) return <p className="text-sm text-muted-foreground">{t('Loading mix…')}</p>
  if (items.length === 0) return <p className="text-sm text-muted-foreground">{t('No kind breakdown yet.')}</p>
  return (
    <div className="grid gap-3">
      {items.map((item, index) => {
        const share = pct(item.event_count, total)
        return (
          <div key={item.kind} className="grid gap-1">
            <div className="flex items-center justify-between text-sm">
              <span className="capitalize">{item.kind}</span>
              <span className="tabular-nums text-muted-foreground">
                {number(item.event_count)} · {number(share / 100, { style: 'percent', maximumFractionDigits: 1 })}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full"
                style={{ width: `${Math.max(share, share > 0 ? 2 : 0)}%`, background: KIND_COLORS[index % KIND_COLORS.length] }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function CompanyList({
  rows,
  loading,
  onOpen,
}: {
  rows: SearchAnalyticsOrg[]
  loading: boolean
  onOpen: (id: string) => void
}) {
  const { t, number } = useTranslation()
  if (loading) return <p className="text-sm text-muted-foreground">{t('Loading companies…')}</p>
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">{t('No per-company volume yet.')}</p>
  const max = Math.max(...rows.map((row) => row.event_count), 1)
  return (
    <ul className="grid gap-2">
      {rows.map((org) => (
        <li key={org.tenant_id ?? org.name}>
          <button
            type="button"
            className="grid w-full gap-1 rounded-xl px-1 py-1.5 text-left hover:bg-muted/60"
            onClick={() => org.tenant_id && onOpen(org.tenant_id)}
            disabled={!org.tenant_id}
          >
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium">{org.name}</span>
              <span className="tabular-nums text-muted-foreground">
                {number(org.event_count)}
                {org.quota_count ? ` · ${t('{count} quota', { count: number(org.quota_count) })}` : ''}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary/70"
                style={{ width: `${Math.max((org.event_count / max) * 100, 2)}%` }}
              />
            </div>
          </button>
        </li>
      ))}
    </ul>
  )
}

function ShareOfPlatform({
  scoped,
  platform,
  loading,
}: {
  scoped?: SearchAnalyticsSummary
  platform?: SearchAnalyticsSummary
  loading: boolean
}) {
  const { t, number } = useTranslation()
  const { rateLabel } = useAnalyticsFormatting()
  if (loading) return <p className="text-sm text-muted-foreground">{t('Loading comparison…')}</p>
  if (!scoped || !platform || platform.event_count === 0) {
    return <p className="text-sm text-muted-foreground">{t('Not enough platform volume to compare.')}</p>
  }
  const rows = [
    {
      label: t('Share of searches'),
      value: rateLabel(scoped.event_count, platform.event_count),
      width: pct(scoped.event_count, platform.event_count),
      hint: t('{count} of {total}', { count: number(scoped.event_count), total: number(platform.event_count) }),
    },
    {
      label: t('Quota pressure'),
      value: rateLabel(scoped.quota_count, scoped.event_count),
      width: pct(scoped.quota_count, scoped.event_count),
      hint: t('platform {rate}', { rate: rateLabel(platform.quota_count, platform.event_count) }),
    },
    {
      label: t('Error rate'),
      value: rateLabel(scoped.error_count, scoped.event_count),
      width: pct(scoped.error_count, scoped.event_count),
      hint: t('platform {rate}', { rate: rateLabel(platform.error_count, platform.event_count) }),
    },
  ]
  return (
    <div className="grid gap-3">
      {rows.map((row) => (
        <div key={row.label} className="grid gap-1">
          <div className="flex items-center justify-between text-sm">
            <span>{row.label}</span>
            <span className="tabular-nums text-muted-foreground">
              {row.value} · {row.hint}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary/70"
              style={{ width: `${Math.max(row.width, row.width > 0 ? 2 : 0)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function TrendList({
  rows,
  loading,
  showOrgs,
}: {
  rows: { query_hash: string; query_normalized: string; kind: string; hits: number; distinct_orgs: number }[]
  loading: boolean
  showOrgs: boolean
}) {
  const { t, number } = useTranslation()
  if (loading) return <p className="text-sm text-muted-foreground">{t('Loading queries…')}</p>
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">{t('No trending queries in this range.')}</p>
  const max = Math.max(...rows.map((row) => row.hits), 1)
  return (
    <ul className="grid gap-3">
      {rows.slice(0, 8).map((row) => (
        <li key={`${row.query_hash}-${row.kind}`} className="grid gap-1">
          <div className="flex items-start justify-between gap-3 text-sm">
            <span className="min-w-0">{row.query_normalized}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {number(row.hits)}
              {showOrgs ? ` · ${t('{count} orgs', { count: number(row.distinct_orgs) })}` : ''}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-chart-2" style={{ width: `${Math.max((row.hits / max) * 100, 2)}%` }} />
          </div>
        </li>
      ))}
    </ul>
  )
}
