import { queryOptions } from '@tanstack/react-query'

import {
  getResourcePressureEpisodes,
  getResourcePressureSummary,
  resourcePressureFiltersSchema,
  type ResourcePressureFilters,
} from '../lib/resource-pressure-api.ts'

export function resourcePressureSummaryOptions(filters: ResourcePressureFilters) {
  const parsed = resourcePressureFiltersSchema.parse(filters)
  return queryOptions({
    queryKey: ['admin-resource-pressure', 'summary', parsed] as const,
    queryFn: ({ signal }) => getResourcePressureSummary(parsed, signal),
    retry: false,
  })
}

export function resourcePressureEpisodesOptions(filters: ResourcePressureFilters, cursor: string | null) {
  const parsed = resourcePressureFiltersSchema.parse(filters)
  return queryOptions({
    queryKey: ['admin-resource-pressure', 'episodes', parsed, cursor] as const,
    queryFn: ({ signal }) => getResourcePressureEpisodes(parsed, cursor, signal),
    retry: false,
  })
}
