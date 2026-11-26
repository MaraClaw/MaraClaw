import type { MultiTenantResponse, NeedsVerificationDetail, TokenResponse, UserOut } from './auth.ts'

// Structural branch guards, not schema parsers. The entry chunk's session store and auth provider use them, so this file
// imports types only: the schemas in auth.ts (zod and the generated contracts) load with the auth client instead.

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
    'user' in value
  )
}

export function userMustChangePassword(user: UserOut | null | undefined): boolean {
  return user?.must_change_password === true
}

export function isNeedsVerificationDetail(value: unknown): value is NeedsVerificationDetail {
  return (
    typeof value === 'object' &&
    value !== null &&
    'needs_verification' in value &&
    value.needs_verification === true
  )
}

export function sessionMustChangePassword(session: TokenResponse): boolean {
  return (
    session.must_change_password === true ||
    session.user?.must_change_password === true ||
    session.identity?.must_change_password === true
  )
}
