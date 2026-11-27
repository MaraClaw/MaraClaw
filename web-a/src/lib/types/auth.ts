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

export type NeedsVerificationDetail = {
  needs_verification: true
  email?: string
  message?: string
}
