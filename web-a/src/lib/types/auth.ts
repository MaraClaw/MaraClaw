export type UserRole = 'platform_admin' | 'org_admin' | 'agent_admin' | 'member' | string

export type IdentityOut = {
  id: string
  email: string | null
  phone: string | null
  username: string | null
  is_active: boolean
  is_platform_admin: boolean
  email_verified: boolean
  must_change_password?: boolean
  created_at: string
  updated_at: string
}

export type UserOut = {
  id: string
  identity_id: string | null
  username: string | null
  email: string | null
  display_name: string
  avatar_url: string | null
  role: UserRole
  is_platform_admin: boolean
  tenant_id: string | null
  title: string | null
  primary_mobile: string | null
  registration_source: string | null
  is_active: boolean
  email_verified: boolean
  must_change_password?: boolean
  created_at: string
}

export type TokenResponse = {
  access_token: string
  token_type: string
  user: UserOut
  identity?: IdentityOut | null
  needs_company_setup?: boolean
  tenant_name?: string | null
  must_change_password?: boolean
}

export type MustChangePasswordDetail = {
  must_change_password: true
  message?: string
}

export function userMustChangePassword(user: UserOut | null | undefined): boolean {
  if (!user) return false
  return user.must_change_password === true
}

export function isMustChangePasswordDetail(value: unknown): value is MustChangePasswordDetail {
  return (
    typeof value === 'object' &&
    value !== null &&
    'must_change_password' in value &&
    (value as MustChangePasswordDetail).must_change_password === true
  )
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
  pending_token?: string | null
}

export type LoginRequest = {
  login_identifier: string
  password: string
  tenant_id?: string | null
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

export function isAdminUser(user: UserOut | null | undefined): boolean {
  if (!user) return false
  if (user.is_platform_admin) return true
  return user.role === 'platform_admin' || user.role === 'org_admin'
}

export function isPlatformAdminUser(user: UserOut | null | undefined): boolean {
  if (!user) return false
  return user.is_platform_admin === true || user.role === 'platform_admin'
}
