import { z } from 'zod'
import { admissionSummarySchema, type AdmissionSummary } from './company-admissions.ts'
import { MemberAuthUserSchema, MemberIdentityOutSchema, MemberRegisterInitResponseSchema, MemberTokenResponseSchema, MultiTenantResponseSchema, UserRoleSchema } from '../engine-contracts.generated.ts'
import type { MemberAuthUser, MemberIdentityOut, MemberTokenResponse, SuggestedOrg as EngineSuggestedOrg, TenantChoice as EngineTenantChoice } from '../engine-contracts.generated.ts'

export type { UserRole } from '../engine-contracts.generated.ts'
export type SuggestedOrg = EngineSuggestedOrg
export type IdentityOut = MemberIdentityOut
export type UserOut = Pick<MemberAuthUser, 'id' | 'display_name' | 'role'> &
  Partial<Pick<MemberAuthUser, 'identity_id' | 'created_at' | 'is_active' | 'must_change_password' | 'timezone'>> & {
    readonly email: string | null
    readonly tenant_id: string | null
    readonly email_verified: boolean
  }
export const userOutSchema = MemberAuthUserSchema.extend({
  email: MemberAuthUserSchema.shape.email.default(null),
  tenant_id: MemberAuthUserSchema.shape.tenant_id.default(null),
  email_verified: MemberAuthUserSchema.shape.email_verified.default(false),
}).strip()
export const memberTokenSchema = MemberTokenResponseSchema.extend({
  user: userOutSchema.nullable().default(null),
  identity: MemberIdentityOutSchema.strip().nullable().optional(),
  message: z.string().optional(),
  admission_request: admissionSummarySchema.nullable().optional(),
}).strip()
export const memberRegisterSchema = MemberRegisterInitResponseSchema.extend({ user: userOutSchema }).strip()
export type RegisterInitResponse = z.infer<typeof memberRegisterSchema>
export const memberMultiTenantSchema = MultiTenantResponseSchema.extend({ requires_tenant_selection: z.literal(true) }).strip()
export const memberLoginSchema = z.union([memberTokenSchema, memberMultiTenantSchema])

const immediateJoinSchema = z.object({
  tenant: z.object({ id: z.uuid(), name: z.string(), slug: z.string() }).readonly(),
  role: UserRoleSchema,
  access_token: z.string().nullable(),
}).readonly()

export const joinOrgResponseSchema = z.union([
  z.object({ status: z.literal('approved'), request: admissionSummarySchema.unwrap().extend({ status: z.literal('approved') }).readonly() }).readonly(),
  z.object({ status: z.literal('pending'), request: admissionSummarySchema }).readonly(),
  immediateJoinSchema,
])

export type JoinOrgResponse = z.infer<typeof joinOrgResponseSchema>

export type TokenResponse = Pick<MemberTokenResponse, 'access_token' | 'token_type' | 'needs_org_confirm' | 'suggested_org' | 'must_change_password'> & {
  readonly user: UserOut | null
  readonly identity?: IdentityOut | null | undefined
  readonly message?: string | undefined
  readonly admission_request?: AdmissionSummary | null | undefined
}
export type TenantChoice = EngineTenantChoice
export type MultiTenantResponse = z.infer<typeof memberMultiTenantSchema>

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
  readonly access_token?: string
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
