import { apiRequest } from '@/lib/http'

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
  agent_count: number
  org_admin_email: string | null
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

export async function listCompanies(q?: string): Promise<CompanyStats[]> {
  const query = q?.trim()
  const suffix = query ? `?q=${encodeURIComponent(query)}` : ''
  return apiRequest<CompanyStats[]>(`/api/admin/companies${suffix}`)
}

export async function createCompany(input: CreateCompanyInput): Promise<CreateCompanyResponse> {
  return apiRequest<CreateCompanyResponse>('/api/admin/companies', {
    method: 'POST',
    body: input,
  })
}

export async function getTenant(tenantId: string): Promise<CompanyStats> {
  const tenant = await apiRequest<{
    id: string
    name: string
    slug: string
    is_active: boolean
    is_system?: boolean
    is_default_end_user_org?: boolean
    can_disable?: boolean
  }>(`/api/tenants/${tenantId}`)
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
    agent_count: 0,
    org_admin_email: null,
  }
}

export async function toggleCompany(companyId: string): Promise<{ ok: boolean; is_active: boolean }> {
  return apiRequest<{ ok: boolean; is_active: boolean }>(`/api/admin/companies/${companyId}/toggle`, {
    method: 'PUT',
  })
}

export async function listEmailDomains(tenantId: string): Promise<EmailDomain[]> {
  return apiRequest<EmailDomain[]>(`/api/tenants/${tenantId}/email-domains`)
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
