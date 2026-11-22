import { infiniteQueryOptions, queryOptions, skipToken, type QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import {
  fetchCompanyAgentSections,
  getCompanyStats,
  getTenant,
  listCompaniesPage,
  listCompanyOptions,
  previewCompanyTier,
  changeCompanyTier,
  type CompanyOption,
  type CompanyTierChange,
  getCompanyRuntime,
  toggleCompany,
} from '../lib/companies-api.ts'
import type { OrganizationPlanId } from '../lib/engine-contracts.generated.ts'
import { ApiError } from '../lib/http.ts'

export function companiesPageOptions(tenantId: string | null, search: string) {
  return infiniteQueryOptions({
    queryKey: ['admin-companies', 'page', tenantId, search],
    initialPageParam: null,
    queryFn: async ({ pageParam, signal }: { readonly pageParam: string | null; readonly signal: AbortSignal }) => {
      try {
        return await listCompaniesPage({ search, cursor: pageParam }, signal)
      } catch (error) {
        if (error instanceof ApiError && error.status === 403 && tenantId) {
          const own = await getTenant(tenantId, signal)
          const needle = search.trim().toLowerCase()
          const matches = `${own.name} ${own.slug}`.toLowerCase().includes(needle)
          return { items: matches ? [own] : [], next_cursor: null }
        }
        throw error
      }
    },
    getNextPageParam: (page) => page.next_cursor,
    retry: false,
  })
}

const companyIdSchema = z.guid()

export function isCompanyId(value: string): boolean {
  return companyIdSchema.safeParse(value).success
}

export function companyOptionsBrowseOptions() {
  return infiniteQueryOptions({
    queryKey: ['admin-companies', 'options', 'browse'],
    initialPageParam: null,
    queryFn: ({ pageParam, signal }: { readonly pageParam: string | null; readonly signal: AbortSignal }) =>
      listCompanyOptions({ q: '', cursor: pageParam }, signal),
    getNextPageParam: (page) => page.next_cursor,
  })
}

export function companyOptionsSearchOptions(search: string) {
  const q = search.trim()
  return queryOptions({
    queryKey: ['admin-companies', 'options', 'search', q],
    queryFn: q ? ({ signal }) => listCompanyOptions({ q, cursor: null }, signal) : skipToken,
  })
}

export function companyOptionByIdOptions(companyId: string) {
  return queryOptions({
    queryKey: ['admin-companies', 'options', 'by-id', companyId],
    queryFn: isCompanyId(companyId)
      ? async ({ signal }): Promise<CompanyOption | null> => {
          try {
            const company = await getTenant(companyId, signal)
            return { id: company.id, name: company.name }
          } catch (error) {
            if (error instanceof ApiError && error.status === 404) return null
            throw error
          }
        }
      : skipToken,
  })
}

export function companyStatsOptions(companyId: string) {
  return queryOptions({
    queryKey: ['admin-companies', 'stats', companyId],
    queryFn: ({ signal }) => getCompanyStats(companyId, signal),
    enabled: Boolean(companyId),
  })
}

export function companyAgentSectionsOptions(companyId: string) {
  return queryOptions({
    queryKey: ['admin-companies', 'agent-sections', companyId],
    queryFn: ({ signal }) => fetchCompanyAgentSections(companyId, signal),
    enabled: Boolean(companyId),
  })
}

export function companyTierPreviewOptions(companyId: string, target: OrganizationPlanId | null) {
  return queryOptions({
    queryKey: ['admin-companies', 'tier-preview', companyId, target],
    queryFn: target === null ? skipToken : ({ signal }) => previewCompanyTier(companyId, target, signal),
    retry: false, staleTime: 0, gcTime: 0,
  })
}

export function companyRuntimeOptions(companyId: string, cursor: string | null) {
  return queryOptions({
    queryKey: ['admin-companies', 'runtime', companyId, cursor],
    queryFn: ({ signal }) => getCompanyRuntime(companyId, cursor, signal),
    enabled: Boolean(companyId), retry: false,
  })
}

export function companyTierChangeOptions(client: QueryClient, companyId: string) {
  return {
    mutationFn: (input: CompanyTierChange) => changeCompanyTier(companyId, input),
    retry: false,
    scope: { id: `company-tier-${companyId}` },
    onSuccess: () => {
      for (const queryKey of [['admin-companies'], ['admin-resource-pressure'], ['admin-capacity']]) {
        void client.invalidateQueries({ queryKey })
      }
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 409) {
        void client.invalidateQueries({ queryKey: ['admin-companies'] })
      }
    },
  }
}

export function companyActivationOptions(client: QueryClient) {
  return {
    mutationFn: toggleCompany, retry: false,
    onSuccess: () => {
      for (const queryKey of [['admin-companies'], ['admin-users'], ['admin-user']]) {
        void client.invalidateQueries({ queryKey })
      }
    },
  }
}
