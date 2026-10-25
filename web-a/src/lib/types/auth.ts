import { z } from 'zod'
import {
  AuthUserSchema,
  MultiTenantResponseSchema,
  TokenResponseSchema,
} from '../engine-contracts.generated.ts'

export type {
  IdentityOut,
  TenantChoice,
  UserRole,
  UserLogin as LoginRequest,
} from '../engine-contracts.generated.ts'

// Admin sessions require a membership; other engine login flows may return no user.
export const adminTokenResponseSchema = TokenResponseSchema.extend({ user: AuthUserSchema })
export const adminMultiTenantResponseSchema = MultiTenantResponseSchema.extend({ requires_tenant_selection: z.literal(true) })
export type TokenResponse = z.infer<typeof adminTokenResponseSchema>
export type UserOut = TokenResponse['user']
export type MultiTenantResponse = z.infer<typeof adminMultiTenantResponseSchema>

export function isGenesisAdmin(user: UserOut | null | undefined): boolean {
  return user?.is_genesis === true
}

export function userMustChangePassword(user: UserOut | null | undefined): boolean {
  if (!user) return false
  return user.must_change_password === true
}

export type NeedsVerificationDetail = {
  needs_verification: true
  email?: string
  message?: string
}

export function isMultiTenantResponse(value: unknown): value is MultiTenantResponse {
  return adminMultiTenantResponseSchema.safeParse(value).success
}

export function isTokenResponse(value: unknown): value is TokenResponse {
  return adminTokenResponseSchema.safeParse(value).success
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

export function canReviewAgentDeletions(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && !isPlatformAdminUser(user) && user.tenant_id != null && user.is_active
}
