import { useQuery } from '@tanstack/react-query'
import { useReducedMotion } from 'framer-motion'
import { useMemo } from 'react'
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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { listCompanies } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import {
  getSearchAnalyticsExportStatus,
  getSearchAnalyticsOrgs,
  getSearchAnalyticsSummary,
  getSearchAnalyticsTimeseries,
  getSearchAnalyticsTrending,
  type SearchAnalyticsOrg,
  type SearchAnalyticsPoint,
  type SearchAnalyticsSummary,
} from '@/lib/search-analytics-api'
import { cn } from '@/lib/utils'

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

function formatCount(value: number | undefined): string {
  if (value == null) return '—'
  return value.toLocaleString()
}

function shortDate(value: string): string {
  const date = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

function rateLabel(part: number, total: number): string {
  return `${pct(part, total)}%`
}

export function SearchEngineAnalytics() {
  const [params, setParams] = useSearchParams()
  const range = asRange(params.get('range'))
  const tenantId = params.get('company') ?? ''
  const reduceMotion = useReducedMotion()
  const window = useMemo(() => rangeFor(range), [range])
  const scoped = { ...window, tenantId: tenantId || undefined }

  const companies = useQuery({ queryKey: ['admin-companies'], queryFn: () => listCompanies() })
  const systemSummary = useQuery({
    queryKey: ['admin-search-analytics-summary', window, 'system'],
    queryFn: () => getSearchAnalyticsSummary(window),
  })
  const summary = useQuery({
    queryKey: ['admin-search-analytics-summary', scoped],
    queryFn: () => getSearchAnalyticsSummary(scoped),
  })
  const series = useQuery({
    queryKey: ['admin-search-analytics-timeseries', scoped],
    queryFn: () => getSearchAnalyticsTimeseries(scoped),
  })
  const orgs = useQuery({
    queryKey: ['admin-search-analytics-orgs', window],
    queryFn: () => getSearchAnalyticsOrgs(window),
  })
  const trending = useQuery({
    queryKey: ['admin-search-analytics-trending', scoped],
    queryFn: () => getSearchAnalyticsTrending(scoped),
  })
  const exportStatus = useQuery({
    queryKey: ['admin-search-analytics-export'],
    queryFn: getSearchAnalyticsExportStatus,
  })

  const error =
    (summary.error instanceof ApiError && summary.error.message) ||
    (series.error instanceof ApiError && series.error.message) ||
    (orgs.error instanceof ApiError && orgs.error.message) ||
    (trending.error instanceof ApiError && trending.error.message) ||
    null

  const systemWide = !tenantId
  const selectedCompany = (companies.data ?? []).find((company) => company.id === tenantId)
  const totals = summary.data
  const platform = systemSummary.data
  const kindMix = totals?.by_kind ?? []
  const chartData = (series.data ?? []).map((point) => ({
    ...point,
    label: shortDate(point.date),
  }))
  const insights = buildInsights({
    systemWide,
    companyName: selectedCompany?.name ?? 'This company',
    scoped: totals,
    platform,
    orgs: orgs.data ?? [],
    trendingTop: trending.data?.[0],
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
              {systemWide ? 'Entire system' : selectedCompany?.name || 'One company'}
            </h2>
            <Badge variant={systemWide ? 'secondary' : 'outline'}>
              {systemWide ? 'All companies' : 'Per organization'}
            </Badge>
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {systemWide
              ? 'Platform search volume, quota pressure, and queries that appear across companies.'
              : 'This company’s share of platform search, errors, and what its agents look up.'}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-sm">
            <span className="text-muted-foreground">Range</span>
            <select
              className="h-10 rounded-xl border border-border bg-card px-3 text-sm"
              value={range}
              onChange={(event) => update({ range: event.target.value as RangeKey })}
            >
              <option value="7">Last 7 days</option>
              <option value="30">Last 30 days</option>
              <option value="90">Last 90 days</option>
            </select>
          </label>
          <label className="grid min-w-56 gap-1 text-sm">
            <span className="text-muted-foreground">Scope</span>
            <select
              className="h-10 rounded-xl border border-border bg-card px-3 text-sm"
              value={tenantId}
              onChange={(event) => update({ company: event.target.value })}
              aria-label="Analytics scope"
            >
              <option value="">Entire system</option>
              {(companies.data ?? []).map((company) => (
                <option key={company.id} value={company.id}>
                  {company.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {error ? <p className="text-sm text-destructive">{error}</p> : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label="Searches"
          value={formatCount(totals?.event_count)}
          hint={
            !systemWide && platform?.event_count
              ? `${rateLabel(totals?.event_count ?? 0, platform.event_count)} of the platform`
              : `${formatCount(totals?.unique_agents)} agents`
          }
          loading={summary.isLoading}
        />
        <KpiCard
          label="Error rate"
          value={totals ? `${rateLabel(totals.error_count, totals.event_count)}` : '—'}
          hint={`${formatCount(totals?.error_count)} failed calls`}
          tone={(totals?.event_count ?? 0) > 0 && pct(totals?.error_count ?? 0, totals?.event_count ?? 0) >= 10 ? 'warn' : 'default'}
          loading={summary.isLoading}
        />
        <KpiCard
          label="Quota pressure"
          value={totals ? `${rateLabel(totals.quota_count, totals.event_count)}` : '—'}
          hint={`${formatCount(totals?.quota_count)} quota responses`}
          tone={(totals?.event_count ?? 0) > 0 && pct(totals?.quota_count ?? 0, totals?.event_count ?? 0) >= 5 ? 'warn' : 'default'}
          loading={summary.isLoading}
        />
        <KpiCard
          label={systemWide ? 'Companies searching' : 'Avg latency'}
          value={
            systemWide
              ? formatCount(totals?.unique_orgs)
              : totals?.avg_latency_ms == null
                ? '—'
                : `${Math.round(totals.avg_latency_ms)} ms`
          }
          hint={
            systemWide
              ? `${formatCount(totals?.unattributed_count)} unattributed`
              : `${formatCount(totals?.unique_agents)} agents in this org`
          }
          loading={summary.isLoading}
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
            <CardTitle>Daily volume</CardTitle>
            <CardDescription>
              {systemWide ? 'Billed Linkup calls across every company.' : 'Billed Linkup calls for this company.'}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <VolumeChart data={chartData} loading={series.isLoading} reduceMotion={Boolean(reduceMotion)} />
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Kind mix</CardTitle>
            <CardDescription>Search vs fetch vs research vs extract.</CardDescription>
          </CardHeader>
          <CardContent>
            <KindMix items={kindMix} total={totals?.event_count ?? 0} loading={summary.isLoading} />
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {systemWide ? (
          <Card>
            <CardHeader>
              <CardTitle>Companies</CardTitle>
              <CardDescription>Open a row to see that organization’s dashboard.</CardDescription>
            </CardHeader>
            <CardContent>
              <CompanyList
                rows={orgs.data ?? []}
                loading={orgs.isLoading}
                onOpen={(id) => update({ company: id })}
              />
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <CardTitle>Share of platform</CardTitle>
              <CardDescription>How this company compares to the entire system in the same range.</CardDescription>
            </CardHeader>
            <CardContent>
              <ShareOfPlatform scoped={totals} platform={platform} loading={summary.isLoading || systemSummary.isLoading} />
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>{systemWide ? 'Queries across companies' : 'What this org searches'}</CardTitle>
            <CardDescription>
              Normalized wording only. Ranked {systemWide ? 'by how many companies share them' : 'by hits'}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <TrendList
              rows={trending.data ?? []}
              loading={trending.isLoading}
              showOrgs={systemWide}
            />
          </CardContent>
        </Card>
      </div>

      {exportStatus.data ? (
        <p className="text-xs text-muted-foreground">
          Warehouse export {exportStatus.data.export_enabled ? 'on' : 'off'}
          {exportStatus.data.bucket ? ` → ${exportStatus.data.prefix}` : ''}.{' '}
          {exportStatus.data.pending} pending.
        </p>
      ) : null}
    </div>
  )
}

export function SearchAnalyticsSnapshot() {
  const window = useMemo(() => rangeFor('7'), [])
  const summary = useQuery({
    queryKey: ['admin-search-analytics-summary', window, 'system'],
    queryFn: () => getSearchAnalyticsSummary(window),
  })
  const orgs = useQuery({
    queryKey: ['admin-search-analytics-orgs', window],
    queryFn: () => getSearchAnalyticsOrgs(window),
  })
  const data = summary.data
  const top = (orgs.data ?? []).slice(0, 3)

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>Search analytics</CardTitle>
          <CardDescription>Last 7 days across the entire system.</CardDescription>
        </div>
        <Link
          to="/search-engine?tab=analytics"
          className="text-sm font-medium text-primary hover:underline"
        >
          Open dashboard
        </Link>
      </CardHeader>
      <CardContent className="grid gap-4">
        {summary.error instanceof ApiError ? (
          <p className="text-sm text-destructive">{summary.error.message}</p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-4">
              <MiniStat label="Searches" value={formatCount(data?.event_count)} />
              <MiniStat label="Error rate" value={data ? rateLabel(data.error_count, data.event_count) : '—'} />
              <MiniStat label="Quota" value={data ? rateLabel(data.quota_count, data.event_count) : '—'} />
              <MiniStat label="Companies" value={formatCount(data?.unique_orgs)} />
            </div>
            {top.length > 0 ? (
              <p className="text-sm text-muted-foreground">
                Busiest: {top.map((org) => org.name).join(', ')}.
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">No billed searches yet this week.</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function buildInsights({
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
  if (!scoped || scoped.event_count === 0) return []
  const lines: string[] = []
  const quotaRate = pct(scoped.quota_count, scoped.event_count)
  const errorRate = pct(scoped.error_count, scoped.event_count)
  const topKind = [...scoped.by_kind].sort((a, b) => b.event_count - a.event_count)[0]
  if (quotaRate >= 5) {
    lines.push(
      systemWide
        ? `Quota responses are ${quotaRate}% of volume. Companies at the top of the list are the upsell candidates.`
        : `${companyName} is hitting quota on ${quotaRate}% of calls.`,
    )
  }
  if (errorRate >= 10) {
    lines.push(`Error rate is ${errorRate}%. Check upstream failures and exhausted keys.`)
  }
  if (topKind && pct(topKind.event_count, scoped.event_count) >= 50) {
    lines.push(`Most calls are ${topKind.kind} (${pct(topKind.event_count, scoped.event_count)}%).`)
  }
  if (systemWide && orgs[0]) {
    lines.push(
      `${orgs[0].name} leads volume with ${orgs[0].event_count.toLocaleString()} searches` +
        (orgs[0].quota_count ? ` and ${orgs[0].quota_count.toLocaleString()} quota hits.` : '.'),
    )
  }
  if (!systemWide && platform?.event_count) {
    lines.push(`${companyName} accounts for ${rateLabel(scoped.event_count, platform.event_count)} of platform searches.`)
  }
  if (trendingTop?.query_normalized) {
    lines.push(
      systemWide && trendingTop.distinct_orgs > 1
        ? `“${trendingTop.query_normalized}” shows up at ${trendingTop.distinct_orgs} companies.`
        : `Top query: “${trendingTop.query_normalized}” (${trendingTop.hits.toLocaleString()} hits).`,
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
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className={cn('text-2xl tabular-nums', tone === 'warn' && 'text-destructive')}>
          {loading ? '…' : value}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-xs text-muted-foreground">{loading ? 'Loading' : hint}</CardContent>
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
  if (loading) return <p className="text-sm text-muted-foreground">Loading chart…</p>
  if (data.length === 0) return <p className="text-sm text-muted-foreground">No search events in this range.</p>
  return (
    <div className="h-56 w-full" role="img" aria-label="Daily search volume">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 12, fill: 'var(--muted-foreground)' }} axisLine={false} tickLine={false} />
          <YAxis allowDecimals={false} tick={{ fontSize: 12, fill: 'var(--muted-foreground)' }} axisLine={false} tickLine={false} width={36} />
          <Tooltip
            contentStyle={{
              background: 'var(--card)',
              border: '1px solid var(--border)',
              borderRadius: 12,
              fontSize: 12,
            }}
            formatter={(value, name) => [Number(value).toLocaleString(), String(name)]}
          />
          <Area
            type="monotone"
            dataKey="event_count"
            name="Searches"
            stroke="var(--chart-1)"
            fill="var(--chart-1)"
            fillOpacity={0.18}
            strokeWidth={2}
            isAnimationActive={!reduceMotion}
          />
          <Area
            type="monotone"
            dataKey="quota_count"
            name="Quota"
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
  if (loading) return <p className="text-sm text-muted-foreground">Loading mix…</p>
  if (items.length === 0) return <p className="text-sm text-muted-foreground">No kind breakdown yet.</p>
  return (
    <div className="grid gap-3">
      {items.map((item, index) => {
        const share = pct(item.event_count, total)
        return (
          <div key={item.kind} className="grid gap-1">
            <div className="flex items-center justify-between text-sm">
              <span className="capitalize">{item.kind}</span>
              <span className="tabular-nums text-muted-foreground">
                {item.event_count.toLocaleString()} · {share}%
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
  if (loading) return <p className="text-sm text-muted-foreground">Loading companies…</p>
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">No per-company volume yet.</p>
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
                {org.event_count.toLocaleString()}
                {org.quota_count ? ` · ${org.quota_count} quota` : ''}
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
  if (loading) return <p className="text-sm text-muted-foreground">Loading comparison…</p>
  if (!scoped || !platform || platform.event_count === 0) {
    return <p className="text-sm text-muted-foreground">Not enough platform volume to compare.</p>
  }
  const rows = [
    { label: 'Searches', part: scoped.event_count, whole: platform.event_count },
    { label: 'Quota hits', part: scoped.quota_count, whole: Math.max(platform.quota_count, 1) },
    { label: 'Errors', part: scoped.error_count, whole: Math.max(platform.error_count, 1) },
  ]
  return (
    <div className="grid gap-3">
      {rows.map((row) => (
        <div key={row.label} className="grid gap-1">
          <div className="flex items-center justify-between text-sm">
            <span>{row.label}</span>
            <span className="tabular-nums text-muted-foreground">{rateLabel(row.part, row.whole)} of system</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary/70" style={{ width: `${Math.max(pct(row.part, row.whole), row.part ? 2 : 0)}%` }} />
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
  if (loading) return <p className="text-sm text-muted-foreground">Loading queries…</p>
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">No trending queries in this range.</p>
  const max = Math.max(...rows.map((row) => row.hits), 1)
  return (
    <ul className="grid gap-3">
      {rows.slice(0, 8).map((row) => (
        <li key={`${row.query_hash}-${row.kind}`} className="grid gap-1">
          <div className="flex items-start justify-between gap-3 text-sm">
            <span className="min-w-0">{row.query_normalized}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {row.hits.toLocaleString()}
              {showOrgs ? ` · ${row.distinct_orgs} orgs` : ''}
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
