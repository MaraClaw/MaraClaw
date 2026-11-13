import { z } from 'zod'

import { apiRequest } from './http.ts'

const companyDeletionPreviewSchema = z.object({
  users_count: z.number().int().nonnegative(),
  agents_count: z.number().int().nonnegative(),
  schedules_count: z.number().int().nonnegative(),
}).readonly()

export type CompanyDeletionPreview = z.infer<typeof companyDeletionPreviewSchema>

const companyDeletionResponseSchema = z.object({
  status: z.literal('deleted'),
  fallback_tenant_id: z.string().nullable(),
}).readonly()

export type CompanyDeletionResponse = z.infer<typeof companyDeletionResponseSchema>

const companyAgentSectionsSchema = z.object({
  sections: z.array(z.string()),
  hidden_sections: z.array(z.string()),
}).readonly()

export type CompanyAgentSections = z.infer<typeof companyAgentSectionsSchema>

export type CompanyStats = {
  id: string
  name: string
  slug: string
  is_active: boolean
  sso_enabled: boolean
  sso_domain: string | null
  is_system: boolean
  is_default_end_user_org: boolean
  can_disable: boolean
  created_at: string | null
  user_count: number
  active_user_count: number
  inactive_user_count: number
  agent_count: number
  org_admin_email: string | null
}

const companyOptionSchema = z.object({
  id: z.string(),
  name: z.string(),
})

const companyStatsSchema = companyOptionSchema.extend({
  slug: z.string(),
  is_active: z.boolean(),
  sso_enabled: z.boolean(),
  sso_domain: z.string().nullable(),
  is_system: z.boolean(),
  is_default_end_user_org: z.boolean(),
  can_disable: z.boolean(),
  created_at: z.string().nullable(),
  user_count: z.number().int().nonnegative(),
  active_user_count: z.number().int().nonnegative(),
  inactive_user_count: z.number().int().nonnegative(),
  agent_count: z.number().int().nonnegative(),
  org_admin_email: z.string().nullable(),
})

const companiesPageSchema = z.object({
  items: z.array(companyStatsSchema),
  next_cursor: z.string().nullable(),
})

export type CompaniesPage = z.infer<typeof companiesPageSchema>
export type CompanyOption = z.infer<typeof companyOptionSchema>

export type CompaniesPageInput = {
  readonly search: string
  readonly cursor: string | null
}

export type EmailDomain = {
  id: string
  tenant_id: string
  domain: string
  is_default: boolean
  created_at: string | null
}

export type CreateCompanyInput = {
  name: string
  admin_email: string
  admin_password: string
  admin_display_name?: string
}

export type CreateCompanyResponse = {
  company: CompanyStats
  org_admin_email: string
  must_change_password: boolean
}

export async function listCompanies(q?: string, signal?: AbortSignal): Promise<CompanyStats[]> {
  const query = q?.trim()
  const suffix = query ? `?q=${encodeURIComponent(query)}` : ''
  return apiRequest<CompanyStats[]>(`/api/admin/companies${suffix}`, { signal })
}

export async function listCompaniesPage(input: CompaniesPageInput, signal?: AbortSignal): Promise<CompaniesPage> {
  const params = new URLSearchParams({ limit: '50' })
  if (input.search.trim()) params.set('q', input.search.trim())
  if (input.cursor !== null) params.set('cursor', input.cursor)
  return apiRequest(`/api/admin/companies/page?${params}`, { schema: companiesPageSchema, signal })
}

export async function listCompanyOptions(signal?: AbortSignal): Promise<CompanyOption[]> {
  return apiRequest('/api/admin/companies/options', { schema: z.array(companyOptionSchema), signal })
}

export async function getCompanyStats(companyId: string, signal?: AbortSignal): Promise<CompanyStats> {
  return apiRequest(`/api/admin/companies/${encodeURIComponent(companyId)}/stats`, { schema: companyStatsSchema, signal })
}

export async function createCompany(input: CreateCompanyInput): Promise<CreateCompanyResponse> {
  return apiRequest<CreateCompanyResponse>('/api/admin/companies', {
    method: 'POST',
    body: input,
  })
}

export async function getTenant(tenantId: string, signal?: AbortSignal): Promise<CompanyStats> {
  const tenant = await apiRequest<{
    id: string
    name: string
    slug: string
    is_active: boolean
    is_system?: boolean
    is_default_end_user_org?: boolean
    can_disable?: boolean
  }>(`/api/tenants/${tenantId}`, { signal })
  const isSystem = Boolean(tenant.is_system)
  const isDefaultEndUserOrg = Boolean(tenant.is_default_end_user_org)
  return {
    id: tenant.id,
    name: tenant.name,
    slug: tenant.slug,
    is_active: tenant.is_active,
    sso_enabled: false,
    sso_domain: null,
    is_system: isSystem,
    is_default_end_user_org: isDefaultEndUserOrg,
    can_disable: tenant.can_disable ?? (!isSystem && !isDefaultEndUserOrg),
    created_at: null,
    user_count: 0,
    active_user_count: 0,
    inactive_user_count: 0,
    agent_count: 0,
    org_admin_email: null,
  }
}

export async function toggleCompany(companyId: string): Promise<{ ok: boolean; is_active: boolean }> {
  return apiRequest<{ ok: boolean; is_active: boolean }>(`/api/admin/companies/${companyId}/toggle`, {
    method: 'PUT',
  })
}

export async function getCompanyDeletionPreview(companyId: string, signal?: AbortSignal): Promise<CompanyDeletionPreview> {
  return apiRequest(
    `/api/tenants/${encodeURIComponent(companyId)}/deletion-preview`, { schema: companyDeletionPreviewSchema, signal },
  )
}

export async function deleteCompany(companyId: string): Promise<CompanyDeletionResponse> {
  return apiRequest(
    `/api/tenants/${encodeURIComponent(companyId)}`, { schema: companyDeletionResponseSchema, method: 'DELETE' },
  )
}

export async function fetchCompanyAgentSections(companyId: string, signal?: AbortSignal): Promise<CompanyAgentSections> {
  return apiRequest(
    `/api/admin/companies/${encodeURIComponent(companyId)}/agent-sections`, { schema: companyAgentSectionsSchema, signal },
  )
}

export async function updateCompanyAgentSections(
  companyId: string,
  hiddenSections: readonly string[],
): Promise<CompanyAgentSections> {
  return apiRequest(`/api/admin/companies/${encodeURIComponent(companyId)}/agent-sections`, {
    schema: companyAgentSectionsSchema,
    method: 'PUT',
    body: { hidden_sections: hiddenSections },
  })
}

export async function listEmailDomains(tenantId: string, signal?: AbortSignal): Promise<EmailDomain[]> {
  return apiRequest<EmailDomain[]>(`/api/tenants/${tenantId}/email-domains`, { signal })
}

export async function addEmailDomain(
  tenantId: string,
  domain: string,
  isDefault = false,
): Promise<EmailDomain> {
  return apiRequest<EmailDomain>(`/api/tenants/${tenantId}/email-domains`, {
    method: 'POST',
    body: { domain, is_default: isDefault },
  })
}

export async function setDefaultEmailDomain(tenantId: string, domainId: string): Promise<EmailDomain> {
  return apiRequest<EmailDomain>(`/api/tenants/${tenantId}/email-domains/${domainId}`, {
    method: 'PATCH',
    body: { is_default: true },
  })
}

export async function deleteEmailDomain(tenantId: string, domainId: string): Promise<void> {
  await apiRequest<void>(`/api/tenants/${tenantId}/email-domains/${domainId}`, { method: 'DELETE' })
}
