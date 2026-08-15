export type UserRole = 'platform_admin' | 'org_admin' | 'agent_admin' | 'member' | string

export type SuggestedOrg = {
  id: string
  name: string
  slug: string
}

export type IdentityOut = {
  id: string
  email?: string | null
  username?: string | null
  is_platform_admin?: boolean
  email_verified?: boolean
  must_change_password?: boolean
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
  identity?: IdentityOut | null
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
  pending_token?: string | null
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

export type MustChangePasswordDetail = {
  must_change_password: true
  message?: string
}

export type AuthProvider = {
  id: string
  provider_type: string
  name: string
  is_active: boolean
}

export type OkMessageResponse = {
  ok: boolean
  message?: string
}

export type ChangePasswordResponse = {
  ok: boolean
  must_change_password?: boolean
  message?: string
}

export type TenantSwitchResponse = {
  access_token: string
  redirect_url?: string | null
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

export function userMustChangePassword(user: UserOut | null | undefined): boolean {
  return user?.must_change_password === true
}

export function isMustChangePasswordDetail(value: unknown): value is MustChangePasswordDetail {
  return (
    typeof value === 'object' &&
    value !== null &&
    'must_change_password' in value &&
    (value as MustChangePasswordDetail).must_change_password === true
  )
}

export function isNeedsVerificationDetail(value: unknown): value is NeedsVerificationDetail {
  return (
    typeof value === 'object' &&
    value !== null &&
    'needs_verification' in value &&
    (value as NeedsVerificationDetail).needs_verification === true
  )
}

export function sessionMustChangePassword(session: TokenResponse): boolean {
  return (
    session.must_change_password === true ||
    session.user.must_change_password === true ||
    session.identity?.must_change_password === true
  )
}
