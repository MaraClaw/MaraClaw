export type UserRole = 'platform_admin' | 'org_admin' | 'agent_admin' | 'member' | string

export type SuggestedOrg = {
  id: string
  name: string
  slug: string
}

export type UserOut = {
  id: string
  identity_id?: string | null
  username: string | null
  email: string | null
  display_name: string
  role: UserRole
  is_platform_admin?: boolean
  tenant_id: string | null
  email_verified: boolean
  must_change_password?: boolean
}

export type TokenResponse = {
  access_token: string
  token_type?: string
  user: UserOut
  needs_company_setup?: boolean
  needs_org_confirm?: boolean
  suggested_org?: SuggestedOrg | null
  must_change_password?: boolean
  message?: string
}

export type TenantChoice = {
  tenant_id: string | null
  tenant_name: string
  tenant_slug: string
  logo_url: string | null
}

export type MultiTenantResponse = {
  requires_tenant_selection: true
  login_identifier: string
  tenants: TenantChoice[]
}

export type LoginRequest = {
  login_identifier: string
  password: string
  tenant_id?: string | null
}

export type EmailLookupResponse = {
  match: SuggestedOrg | null
  fallback: SuggestedOrg | null
}

export type NeedsVerificationDetail = {
  needs_verification: true
  email?: string
  message?: string
}

export function isMultiTenantResponse(value: unknown): value is MultiTenantResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'requires_tenant_selection' in value &&
    (value as MultiTenantResponse).requires_tenant_selection === true
  )
}

export function isTokenResponse(value: unknown): value is TokenResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'access_token' in value &&
    typeof (value as TokenResponse).access_token === 'string' &&
    'user' in value
  )
}

export function isPlatformAdminUser(user: UserOut | null | undefined): boolean {
  if (!user) return false
  return user.is_platform_admin === true || user.role === 'platform_admin'
}
