import { z } from 'zod'

import { apiRequest } from './http.ts'
import {
  OrganizationPlanIdValues,
  PressureKindValues,
  PressureResourceValues,
  ResourcePressureEpisodeOutSchema,
  ResourcePressureEpisodePageSchema,
  ResourcePressureTenantSummarySchema,
} from './engine-contracts.generated.ts'

export const RESOURCE_PRESSURE_EPISODE_PAGE_SIZE = 25
export const RESOURCE_PRESSURE_CSV_FILENAME = 'resource-pressure-episodes.csv'
const MAX_CURSOR_LENGTH = 512

/** `since` is inclusive and `until` exclusive, both RFC 3339 instants; the episode-only fields are ignored by the summary. */
export const resourcePressureFiltersSchema = z.strictObject({
  since: z.iso.datetime({ offset: true }).optional(),
  until: z.iso.datetime({ offset: true }).optional(),
  tier: z.enum(OrganizationPlanIdValues).optional(),
  resource: z.enum(PressureResourceValues).optional(),
  tenantId: z.guid().optional(),
  agentId: z.guid().optional(),
  kind: z.enum(PressureKindValues).optional(),
  openOnly: z.boolean().optional(),
}).readonly()
export type ResourcePressureFilters = z.input<typeof resourcePressureFiltersSchema>
type ParsedFilters = z.output<typeof resourcePressureFiltersSchema>

const summarySchema = ResourcePressureTenantSummarySchema.strip()
const episodeSchema = ResourcePressureEpisodeOutSchema.strip()
const episodePageSchema = ResourcePressureEpisodePageSchema.strip().extend({
  items: z.array(episodeSchema),
  next_cursor: z.string().min(1).max(MAX_CURSOR_LENGTH).nullable(),
})
const csvSchema = z.string()

export type ResourcePressureSummary = z.infer<typeof summarySchema>
export type ResourcePressureEpisode = z.infer<typeof episodeSchema>
export type ResourcePressureEpisodePage = z.infer<typeof episodePageSchema>

export function resourcePressureSummaryParams(filters: ParsedFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.since) params.set('since', filters.since)
  if (filters.until) params.set('until', filters.until)
  if (filters.tier) params.set('tier', filters.tier)
  if (filters.resource) params.set('resource', filters.resource)
  if (filters.tenantId) params.set('tenant_id', filters.tenantId)
  return params
}

export function resourcePressureEpisodeParams(filters: ParsedFilters): URLSearchParams {
  const params = resourcePressureSummaryParams(filters)
  if (filters.agentId) params.set('agent_id', filters.agentId)
  if (filters.kind) params.set('kind', filters.kind)
  if (filters.openOnly) params.set('open_only', 'true')
  return params
}

export async function getResourcePressureSummary(
  filters: ResourcePressureFilters,
  signal?: AbortSignal,
): Promise<ResourcePressureSummary[]> {
  const parsed = resourcePressureFiltersSchema.parse(filters)
  const query = resourcePressureSummaryParams(parsed).toString()
  const suffix = query ? `?${query}` : ''
  return apiRequest(`/api/admin/resource-pressure/summary${suffix}`, { schema: summarySchema.array(), signal })
}

export async function getResourcePressureEpisodes(
  filters: ResourcePressureFilters,
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<ResourcePressureEpisodePage> {
  const parsed = resourcePressureFiltersSchema.parse(filters)
  const params = resourcePressureEpisodeParams(parsed)
  params.set('limit', String(RESOURCE_PRESSURE_EPISODE_PAGE_SIZE))
  if (cursor) params.set('cursor', z.string().max(MAX_CURSOR_LENGTH).parse(cursor))
  return apiRequest(`/api/admin/resource-pressure/episodes?${params}`, { schema: episodePageSchema, signal })
}

/**
 * The export is a plain Bearer-authenticated GET (no download ticket), so the browser cannot navigate to it.
 * `apiRequest` supplies the auth header, session fencing and 401 policy; a CSV body never parses as JSON
 * (it starts with the `id,` header row), so the client returns it as text.
 */
export async function exportResourcePressureCsv(filters: ResourcePressureFilters, signal?: AbortSignal): Promise<string> {
  const parsed = resourcePressureFiltersSchema.parse(filters)
  const query = resourcePressureEpisodeParams(parsed).toString()
  const suffix = query ? `?${query}` : ''
  return apiRequest(`/api/admin/resource-pressure/export.csv${suffix}`, { schema: csvSchema, signal })
}
