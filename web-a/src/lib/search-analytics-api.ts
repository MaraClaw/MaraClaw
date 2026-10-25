import { apiRequest } from './http.ts'
import { z } from 'zod'
import {
  SearchAnalyticsExportStatusSchema, SearchAnalyticsOrgRankingResponseSchema,
  SearchAnalyticsKindCountResponseSchema, SearchAnalyticsSummaryResponseSchema, SearchAnalyticsTimeBucketResponseSchema,
  SearchAnalyticsTrendingQueryResponseSchema,
} from './engine-contracts.generated.ts'

const kindCountSchema = SearchAnalyticsKindCountResponseSchema.transform((value) => ({
  ...value, kind: value.kind ?? '', event_count: value.event_count ?? 0,
}))
const summarySchema = SearchAnalyticsSummaryResponseSchema.extend({ by_kind: kindCountSchema.array().optional() })
  .transform((value) => ({
    ...value, event_count: value.event_count ?? 0, error_count: value.error_count ?? 0,
    quota_count: value.quota_count ?? 0, unique_orgs: value.unique_orgs ?? 0,
    unique_agents: value.unique_agents ?? 0, unattributed_count: value.unattributed_count ?? 0,
    avg_latency_ms: value.avg_latency_ms ?? 0, by_kind: value.by_kind ?? [],
  }))
const pointSchema = SearchAnalyticsTimeBucketResponseSchema.transform((value) => ({
  ...value, date: value.date ?? '', event_count: value.event_count ?? 0,
  error_count: value.error_count ?? 0, quota_count: value.quota_count ?? 0,
}))
const orgSchema = SearchAnalyticsOrgRankingResponseSchema.transform((value) => ({
  ...value, tenant_id: value.tenant_id ?? null, name: value.name ?? '',
  event_count: value.event_count ?? 0, quota_count: value.quota_count ?? 0,
}))
const trendSchema = SearchAnalyticsTrendingQueryResponseSchema.transform((value) => ({
  ...value, query_hash: value.query_hash ?? '', query_normalized: value.query_normalized ?? '',
  kind: value.kind ?? '', hits: value.hits ?? 0, distinct_agents: value.distinct_agents ?? 0,
  distinct_orgs: value.distinct_orgs ?? 0,
}))
const exportStatusSchema = SearchAnalyticsExportStatusSchema.transform((value) => ({
  ...value, pending: value.pending ?? 0, exporting: value.exporting ?? 0,
  exported: value.exported ?? 0, skipped: value.skipped ?? 0, last_exported_at: value.last_exported_at ?? null,
}))
export type SearchAnalyticsSummary = z.infer<typeof summarySchema>
export type SearchAnalyticsPoint = z.infer<typeof pointSchema>
export type SearchAnalyticsOrg = z.infer<typeof orgSchema>
export type SearchAnalyticsTrend = z.infer<typeof trendSchema>
export type SearchAnalyticsExportStatus = z.infer<typeof exportStatusSchema>

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
  signal?: AbortSignal,
): Promise<SearchAnalyticsSummary> {
  return apiRequest(`/api/admin/search-analytics/summary${rangeQuery(params)}`, { schema: summarySchema, signal })
}

export async function getSearchAnalyticsTimeseries(
  params: SearchAnalyticsQuery,
  signal?: AbortSignal,
): Promise<SearchAnalyticsPoint[]> {
  return apiRequest(`/api/admin/search-analytics/timeseries${rangeQuery(params)}`, { schema: pointSchema.array(), signal })
}

export async function getSearchAnalyticsOrgs(
  params: SearchAnalyticsQuery,
  signal?: AbortSignal,
): Promise<SearchAnalyticsOrg[]> {
  return apiRequest(`/api/admin/search-analytics/orgs${rangeQuery(params)}`, { schema: orgSchema.array(), signal })
}

export async function getSearchAnalyticsTrending(
  params: SearchAnalyticsQuery,
  signal?: AbortSignal,
): Promise<SearchAnalyticsTrend[]> {
  return apiRequest(
    `/api/admin/search-analytics/trending${rangeQuery(params)}`,
    { schema: trendSchema.array(), signal },
  )
}

export async function getSearchAnalyticsExportStatus(signal?: AbortSignal): Promise<SearchAnalyticsExportStatus> {
  return apiRequest('/api/admin/search-analytics/export-status', { schema: exportStatusSchema, signal })
}
