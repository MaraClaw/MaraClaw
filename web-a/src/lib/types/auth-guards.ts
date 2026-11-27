import type { MultiTenantResponse, TokenResponse, UserOut } from './auth.ts'

// Role predicates and response branch guards. The entry chunk's session store and auth provider use them, so this file
// imports types only: the schemas in auth.ts (zod and the generated contracts) load with the auth client instead.
// The branch guards are structural; the auth client has already parsed the response with the schemas.

export function isGenesisAdmin(user: UserOut | null | undefined): boolean {
  return user?.is_genesis === true
}

export function userMustChangePassword(user: UserOut | null | undefined): boolean {
  if (!user) return false
  return user.must_change_password === true
}

export function isMultiTenantResponse(value: unknown): value is MultiTenantResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'requires_tenant_selection' in value &&
    value.requires_tenant_selection === true
  )
}

export function isTokenResponse(value: unknown): value is TokenResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'access_token' in value &&
    typeof value.access_token === 'string' &&
    'user' in value &&
    typeof value.user === 'object' &&
    value.user !== null
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

export function canSendBroadcast(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && !isPlatformAdminUser(user)
}

export function canManageInvitations(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && !isPlatformAdminUser(user) && user.tenant_id != null
}

export function canReviewAdmissions(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && user.is_genesis === true
}

export function canReviewOrganizationAdmissions(user: UserOut | null | undefined): boolean {
  return user?.role === 'platform_admin' && user.is_genesis === true && user.is_active === true
}

export function canReviewAgentDeletions(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && !isPlatformAdminUser(user) && user.tenant_id != null && user.is_active
}

export function canViewClawbook(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && !isPlatformAdminUser(user) && user.tenant_id != null && user.is_active === true
}

export function canBuyCapacity(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && !isPlatformAdminUser(user) && user.tenant_id != null && user.is_active === true
}

export function canReviewCapacityPayments(user: UserOut | null | undefined): boolean {
  return user?.is_active === true && isPlatformAdminUser(user)
}

export function canViewCapacityOrders(user: UserOut | null | undefined): boolean {
  return canBuyCapacity(user) || canReviewCapacityPayments(user)
}

export function canManageCapacityPaymentSettings(user: UserOut | null | undefined): boolean {
  return canReviewOrganizationAdmissions(user)
}
