import { apiRequest } from './http.ts'
import { z } from 'zod'
import type { SessionOwner } from './auth-session.ts'
import { joinOrgResponseSchema } from './types/auth.ts'
import { admissionSummarySchema } from './types/company-admissions.ts'
import { paymentReviewResponseSchema, type TransferInput } from './types/company-transfers.ts'
import type {
  AuthProvider,
  ChangePasswordResponse,
  EmailLookupResponse,
  JoinOrgResponse,
  LoginRequest,
  MultiTenantResponse,
  OkMessageResponse,
  TenantChoice,
  TenantSwitchResponse,
  TokenResponse,
  UserOut,
} from './types/auth.ts'

function requestContext(owner?: SessionOwner, signal?: AbortSignal) {
  return {
    ...(owner === undefined ? {} : { owner }),
    ...(signal === undefined ? {} : { signal }),
  }
}

const chatTicketSchema = z.object({ ticket: z.string().min(1), session_id: z.string().min(1) })
export type ChatTicket = z.infer<typeof chatTicketSchema>
export type ChatTicketRequest = { readonly agent_id: string; readonly session_id?: string }

export async function issueChatTicket(body: ChatTicketRequest, owner: SessionOwner, signal: AbortSignal): Promise<ChatTicket> {
  return chatTicketSchema.parse(await apiRequest<unknown>('/api/auth/chat-ticket', {
    method: 'POST', body, ...requestContext(owner, signal),
  }))
}

export async function loginRequest(
  body: LoginRequest,
  signal?: AbortSignal,
  owner?: SessionOwner,
): Promise<TokenResponse | MultiTenantResponse> {
  return apiRequest<TokenResponse | MultiTenantResponse>('/api/auth/login', {
    method: 'POST',
    body,
    token: null,
    ...requestContext(owner, signal),
  })
}

export async function registerRequest(
  body: {
    email: string
    display_name?: string
    password: string
    invitation_code?: string
  },
  signal?: AbortSignal,
  owner?: SessionOwner,
): Promise<TokenResponse> {
  const { admission_request, ...session } = await apiRequest<Omit<TokenResponse, 'admission_request'> & { admission_request?: unknown }>('/api/auth/register/init', {
    method: 'POST',
    body,
    token: null,
    ...requestContext(owner, signal),
  })
  return admission_request === undefined ? session : { ...session, admission_request: admissionSummarySchema.nullable().parse(admission_request) }
}

export async function fetchCurrentUser(token: string, signal?: AbortSignal, owner?: SessionOwner): Promise<UserOut> {
  return apiRequest<UserOut>('/api/auth/me', {
    method: 'GET',
    token,
    ...requestContext(owner, signal),
  })
}

export async function updateCurrentUser(
  body: { display_name?: string; email?: string; timezone?: string | null },
  owner?: SessionOwner,
): Promise<UserOut> {
  return apiRequest<UserOut>('/api/auth/me', {
    method: 'PATCH',
    body,
    ...requestContext(owner),
  })
}

export async function lookupOrgByEmail(email: string, owner?: SessionOwner): Promise<EmailLookupResponse> {
  return apiRequest<EmailLookupResponse>(
    `/api/tenants/lookup-by-email?email=${encodeURIComponent(email)}`,
    requestContext(owner),
  )
}

export async function joinSuggestedOrg(tenantId: string, owner?: SessionOwner): Promise<JoinOrgResponse> {
  return joinOrgResponseSchema.parse(await apiRequest<unknown>('/api/tenants/join-suggested', {
    method: 'POST',
    body: { tenant_id: tenantId },
    ...requestContext(owner),
  }))
}

export async function joinDefaultOrg(owner?: SessionOwner): Promise<JoinOrgResponse> {
  return joinOrgResponseSchema.parse(await apiRequest<unknown>('/api/tenants/join-default', {
    method: 'POST',
    ...requestContext(owner),
  }))
}

export async function transferOrg(body: TransferInput, owner?: SessionOwner) {
  return z.union([paymentReviewResponseSchema, joinOrgResponseSchema]).parse(await apiRequest<unknown>('/api/tenants/transfer', {
    method: 'POST',
    body,
    ...requestContext(owner),
  }))
}

export async function forgotPasswordRequest(email: string, signal?: AbortSignal): Promise<OkMessageResponse> {
  return apiRequest<OkMessageResponse>('/api/auth/forgot-password', {
    method: 'POST',
    body: { email },
    token: null,
    ...requestContext(undefined, signal),
  })
}

export async function resetPasswordRequest(
  body: { token: string; new_password: string },
  signal?: AbortSignal,
): Promise<OkMessageResponse> {
  return apiRequest<OkMessageResponse>('/api/auth/reset-password', {
    method: 'POST',
    body,
    token: null,
    ...requestContext(undefined, signal),
  })
}

export async function changePasswordRequest(body: {
  old_password: string
  new_password: string
}, owner?: SessionOwner): Promise<ChangePasswordResponse> {
  return apiRequest<ChangePasswordResponse>('/api/auth/me/password', {
    method: 'PUT',
    body,
    ...requestContext(owner),
  })
}

export async function verifyEmailRequest(token: string, signal?: AbortSignal, owner?: SessionOwner): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/auth/verify-email', {
    method: 'POST',
    body: { token },
    token: null,
    ...requestContext(owner, signal),
  })
}

export async function resendVerificationRequest(email: string, owner?: SessionOwner): Promise<OkMessageResponse> {
  return apiRequest<OkMessageResponse>('/api/auth/resend-verification', {
    method: 'POST',
    body: { email },
    token: null,
    ...requestContext(owner),
  })
}

export async function fetchRegistrationConfig(owner?: SessionOwner): Promise<{ invitation_code_required: boolean }> {
  return apiRequest<{ invitation_code_required: boolean }>('/api/auth/registration-config', {
    token: null,
    ...requestContext(owner),
  })
}

export async function checkDuplicate(params: {
  email: string
}, owner?: SessionOwner): Promise<{ email_exists: boolean; has_conflict: boolean }> {
  const query = new URLSearchParams({ email: params.email })
  return apiRequest(`/api/auth/check-duplicate?${query.toString()}`, { token: null, ...requestContext(owner) })
}

export async function listAuthProviders(tenantId?: string | null, owner?: SessionOwner): Promise<AuthProvider[]> {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return apiRequest<AuthProvider[]>(`/api/auth/providers${query}`, { token: null, ...requestContext(owner) })
}

export async function authorizeProvider(
  provider: string,
  redirectUri: string,
  authorization: string | { readonly state: string; readonly owner: SessionOwner },
): Promise<{ authorization_url: string }> {
  const state = typeof authorization === 'string' ? authorization : authorization.state
  const owner = typeof authorization === 'string' ? undefined : authorization.owner
  const query = new URLSearchParams({ redirect_uri: redirectUri, state })
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/authorize?${query.toString()}`, {
    token: null,
    ...requestContext(owner),
  })
}

export async function oauthCallback(
  provider: string,
  body: { code?: string; pending_token?: string; tenant_id?: string },
  owner?: SessionOwner,
): Promise<TokenResponse | MultiTenantResponse> {
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/callback`, {
    method: 'POST',
    body,
    token: null,
    ...requestContext(owner),
  })
}

export async function bindProvider(provider: string, code: string, owner?: SessionOwner): Promise<UserOut> {
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/bind`, {
    method: 'POST',
    body: { provider_type: provider, code },
    ...requestContext(owner),
  })
}

export async function unbindProvider(provider: string, owner?: SessionOwner): Promise<UserOut> {
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/unbind`, {
    method: 'POST',
    body: { provider_type: provider },
    ...requestContext(owner),
  })
}

export async function fetchMyTenants(owner?: SessionOwner): Promise<TenantChoice[]> {
  return apiRequest<TenantChoice[]>('/api/auth/my-tenants', requestContext(owner))
}

export async function switchTenant(tenantId: string, owner?: SessionOwner): Promise<TenantSwitchResponse> {
  const result = z.object({ access_token: z.string().min(1), redirect_url: z.string().nullable().optional(), message: z.string().optional() }).parse(await apiRequest<unknown>('/api/auth/switch-tenant', {
    method: 'POST',
    body: { tenant_id: tenantId },
    ...requestContext(owner),
  }))
  return {
    access_token: result.access_token,
    ...(result.redirect_url === undefined ? {} : { redirect_url: result.redirect_url }),
    ...(result.message === undefined ? {} : { message: result.message }),
  }
}

export async function fetchMyTenant(): Promise<{
  id: string
  name: string
  slug: string
}> {
  return apiRequest('/api/tenants/me')
}
