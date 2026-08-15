import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { listCompanies } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import {
  getSearchAnalyticsExportStatus,
  getSearchAnalyticsOrgs,
  getSearchAnalyticsSummary,
  getSearchAnalyticsTimeseries,
  getSearchAnalyticsTrending,
} from '@/lib/search-analytics-api'

type RangeKey = '7' | '30' | '90'

function rangeFor(key: RangeKey): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end.getTime() - Number(key) * 24 * 60 * 60 * 1000)
  return { start: start.toISOString(), end: end.toISOString() }
}

export function SearchEngineAnalytics() {
  const [range, setRange] = useState<RangeKey>('7')
  const [tenantId, setTenantId] = useState('')
  const window = useMemo(() => rangeFor(range), [range])
  const params = { ...window, tenantId: tenantId || undefined }

  const companies = useQuery({ queryKey: ['admin-companies'], queryFn: () => listCompanies() })
  const summary = useQuery({
    queryKey: ['admin-search-analytics-summary', params],
    queryFn: () => getSearchAnalyticsSummary(params),
  })
  const series = useQuery({
    queryKey: ['admin-search-analytics-timeseries', params],
    queryFn: () => getSearchAnalyticsTimeseries(params),
  })
  const orgs = useQuery({
    queryKey: ['admin-search-analytics-orgs', window],
    queryFn: () => getSearchAnalyticsOrgs(window),
  })
  const trending = useQuery({
    queryKey: ['admin-search-analytics-trending', params],
    queryFn: () => getSearchAnalyticsTrending(params),
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
  const kindMix = summary.data?.by_kind ?? []

  return (
    <div className="grid gap-4">
      <div>
        <p className="text-sm font-medium">{systemWide ? 'Entire system' : 'One company'}</p>
        <p className="mt-1 text-sm text-muted-foreground">
          {systemWide
            ? 'All organizations together — volume, kind mix, and queries that show up across companies.'
            : 'Search activity for the selected company only.'}
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-sm">
          <span className="text-muted-foreground">Range</span>
          <select
            className="h-10 rounded-xl border border-border bg-card px-3 text-sm"
            value={range}
            onChange={(event) => setRange(event.target.value as RangeKey)}
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
            onChange={(event) => setTenantId(event.target.value)}
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

      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      {systemWide && (summary.data?.unattributed_count ?? 0) > 0 ? (
        <p className="text-sm text-muted-foreground">
          {summary.data?.unattributed_count} events have no company (agent without a tenant).
        </p>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <StatCard label="Searches" value={summary.data?.event_count} />
        <StatCard label="Errors" value={summary.data?.error_count} />
        <StatCard label="Quota hits" value={summary.data?.quota_count} />
        <StatCard label="Companies" value={summary.data?.unique_orgs} />
        <StatCard label="Agents" value={summary.data?.unique_agents} />
        <StatCard
          label="Avg latency (ms)"
          value={
            summary.data?.avg_latency_ms == null ? undefined : Math.round(summary.data.avg_latency_ms)
          }
        />
      </div>

      {kindMix.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Kind mix</CardTitle>
            <CardDescription>
              {systemWide
                ? 'How the whole platform uses search, fetch, research, and extract.'
                : 'How this company uses each Linkup API.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-3 text-sm">
            {kindMix.map((item) => (
              <span key={item.kind} className="rounded-xl bg-muted px-3 py-1.5">
                {item.kind} · {item.event_count}
              </span>
            ))}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Daily volume</CardTitle>
          <CardDescription>
            {systemWide ? 'Platform-wide billed Linkup calls.' : 'Billed Linkup calls for this company.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {(series.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No search events in this range.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-4 font-medium">Date</th>
                    <th className="py-2 pr-4 font-medium">Events</th>
                    <th className="py-2 pr-4 font-medium">Errors</th>
                    <th className="py-2 font-medium">Quota</th>
                  </tr>
                </thead>
                <tbody>
                  {(series.data ?? []).map((point) => (
                    <tr key={point.date} className="border-t border-border/70">
                      <td className="py-2 pr-4">{point.date}</td>
                      <td className="py-2 pr-4">{point.event_count}</td>
                      <td className="py-2 pr-4">{point.error_count}</td>
                      <td className="py-2">{point.quota_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {systemWide ? (
      <Card>
        <CardHeader>
          <CardTitle>Top companies</CardTitle>
          <CardDescription>Volume and quota pressure across the system — useful for upsell.</CardDescription>
        </CardHeader>
        <CardContent>
          {(orgs.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No per-company volume yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-4 font-medium">Company</th>
                    <th className="py-2 pr-4 font-medium">Events</th>
                    <th className="py-2 font-medium">Quota</th>
                  </tr>
                </thead>
                <tbody>
                  {(orgs.data ?? []).map((org) => (
                    <tr key={org.tenant_id ?? org.name} className="border-t border-border/70">
                      <td className="py-2 pr-4">{org.name}</td>
                      <td className="py-2 pr-4">{org.event_count}</td>
                      <td className="py-2">{org.quota_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>{systemWide ? 'System trending queries' : 'Trending queries'}</CardTitle>
          <CardDescription>
            {systemWide
              ? 'Normalized queries ranked by how many companies use them, then by hits. Raw text is not stored here.'
              : 'Normalized wording for this company only. Raw search text is not stored in the console.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {(trending.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No trending queries in this range.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-4 font-medium">Query</th>
                    <th className="py-2 pr-4 font-medium">Kind</th>
                    <th className="py-2 pr-4 font-medium">Hits</th>
                    <th className="py-2 font-medium">Orgs</th>
                  </tr>
                </thead>
                <tbody>
                  {(trending.data ?? []).map((row) => (
                    <tr key={`${row.query_hash}-${row.kind}`} className="border-t border-border/70">
                      <td className="py-2 pr-4">{row.query_normalized}</td>
                      <td className="py-2 pr-4">{row.kind}</td>
                      <td className="py-2 pr-4">{row.hits}</td>
                      <td className="py-2">{row.distinct_orgs}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

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

function StatCard({ label, value }: { label: string; value: number | undefined }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl">{value ?? '—'}</CardTitle>
      </CardHeader>
    </Card>
  )
}
