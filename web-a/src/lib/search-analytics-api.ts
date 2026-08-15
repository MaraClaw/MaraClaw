import { apiRequest } from '@/lib/http'

export type SearchAnalyticsSummary = {
  event_count: number
  error_count: number
  quota_count: number
  unique_orgs: number
  unique_agents: number
  unattributed_count: number
  avg_latency_ms: number
  by_kind: { kind: string; event_count: number }[]
  scope: 'system' | 'org'
}

export type SearchAnalyticsPoint = {
  date: string
  event_count: number
  error_count: number
  quota_count: number
}

export type SearchAnalyticsOrg = {
  tenant_id: string | null
  name: string
  event_count: number
  quota_count: number
}

export type SearchAnalyticsTrend = {
  query_hash: string
  query_normalized: string
  kind: string
  hits: number
  distinct_agents: number
  distinct_orgs: number
}

export type SearchAnalyticsExportStatus = {
  pending: number
  exporting: number
  exported: number
  skipped: number
  last_exported_at: string | null
  export_enabled: boolean
  include_raw: boolean
  capture_enabled: boolean
  bucket: string | null
  prefix: string
}

export type SearchAnalyticsQuery = {
  start?: string
  end?: string
  tenantId?: string
}

function rangeQuery(params: SearchAnalyticsQuery, extra?: Record<string, string>): string {
  const search = new URLSearchParams()
  if (params.start) search.set('start', params.start)
  if (params.end) search.set('end', params.end)
  if (params.tenantId) search.set('tenant_id', params.tenantId)
  if (extra) {
    for (const [key, value] of Object.entries(extra)) search.set(key, value)
  }
  const suffix = search.toString()
  return suffix ? `?${suffix}` : ''
}

export async function getSearchAnalyticsSummary(
  params: SearchAnalyticsQuery,
): Promise<SearchAnalyticsSummary> {
  return apiRequest<SearchAnalyticsSummary>(`/api/admin/search-analytics/summary${rangeQuery(params)}`)
}

export async function getSearchAnalyticsTimeseries(
  params: SearchAnalyticsQuery,
): Promise<SearchAnalyticsPoint[]> {
  return apiRequest<SearchAnalyticsPoint[]>(`/api/admin/search-analytics/timeseries${rangeQuery(params)}`)
}

export async function getSearchAnalyticsOrgs(
  params: SearchAnalyticsQuery,
): Promise<SearchAnalyticsOrg[]> {
  return apiRequest<SearchAnalyticsOrg[]>(`/api/admin/search-analytics/orgs${rangeQuery(params)}`)
}

export async function getSearchAnalyticsTrending(
  params: SearchAnalyticsQuery,
): Promise<SearchAnalyticsTrend[]> {
  return apiRequest<SearchAnalyticsTrend[]>(
    `/api/admin/search-analytics/trending${rangeQuery(params)}`,
  )
}

export async function getSearchAnalyticsExportStatus(): Promise<SearchAnalyticsExportStatus> {
  return apiRequest<SearchAnalyticsExportStatus>('/api/admin/search-analytics/export-status')
}
