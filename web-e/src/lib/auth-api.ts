import { apiRequest } from '@/lib/http'
import type {
  AuthProvider,
  ChangePasswordResponse,
  EmailLookupResponse,
  LoginRequest,
  MultiTenantResponse,
  OkMessageResponse,
  SuggestedOrg,
  TenantChoice,
  TenantSwitchResponse,
  TokenResponse,
  UserOut,
} from '@/lib/types/auth'

export async function loginRequest(
  body: LoginRequest,
  signal?: AbortSignal,
): Promise<TokenResponse | MultiTenantResponse> {
  return apiRequest<TokenResponse | MultiTenantResponse>('/api/auth/login', {
    method: 'POST',
    body,
    token: null,
    signal,
  })
}

export async function registerRequest(
  body: {
    email: string
    username: string
    password: string
    invitation_code?: string
  },
  signal?: AbortSignal,
): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/auth/register/init', {
    method: 'POST',
    body,
    token: null,
    signal,
  })
}

export async function fetchCurrentUser(token: string, signal?: AbortSignal): Promise<UserOut> {
  return apiRequest<UserOut>('/api/auth/me', {
    method: 'GET',
    token,
    signal,
  })
}

export async function updateCurrentUser(
  body: { display_name?: string; username?: string; email?: string },
): Promise<UserOut> {
  return apiRequest<UserOut>('/api/auth/me', {
    method: 'PATCH',
    body,
  })
}

export async function lookupOrgByEmail(email: string): Promise<EmailLookupResponse> {
  return apiRequest<EmailLookupResponse>(
    `/api/tenants/lookup-by-email?email=${encodeURIComponent(email)}`,
  )
}

export async function joinSuggestedOrg(tenantId: string): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/tenants/join-suggested', {
    method: 'POST',
    body: { tenant_id: tenantId },
  })
}

export async function joinDefaultOrg(): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/tenants/join-default', {
    method: 'POST',
  })
}

export async function transferOrg(body: {
  password: string
  invitation_code?: string
  tenant_id?: string
}): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/tenants/transfer', {
    method: 'POST',
    body,
  })
}

export async function joinWithInvite(invitationCode: string): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/tenants/join', {
    method: 'POST',
    body: { invitation_code: invitationCode },
  })
}

export async function forgotPasswordRequest(email: string, signal?: AbortSignal): Promise<OkMessageResponse> {
  return apiRequest<OkMessageResponse>('/api/auth/forgot-password', {
    method: 'POST',
    body: { email },
    token: null,
    signal,
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
    signal,
  })
}

export async function changePasswordRequest(body: {
  old_password: string
  new_password: string
}): Promise<ChangePasswordResponse> {
  return apiRequest<ChangePasswordResponse>('/api/auth/me/password', {
    method: 'PUT',
    body,
  })
}

export async function verifyEmailRequest(token: string, signal?: AbortSignal): Promise<TokenResponse> {
  return apiRequest<TokenResponse>('/api/auth/verify-email', {
    method: 'POST',
    body: { token },
    token: null,
    signal,
  })
}

export async function resendVerificationRequest(email: string): Promise<OkMessageResponse> {
  return apiRequest<OkMessageResponse>('/api/auth/resend-verification', {
    method: 'POST',
    body: { email },
    token: null,
  })
}

export async function fetchRegistrationConfig(): Promise<{ invitation_code_required: boolean }> {
  return apiRequest<{ invitation_code_required: boolean }>('/api/auth/registration-config', {
    token: null,
  })
}

export async function checkDuplicate(params: {
  email?: string
  username?: string
}): Promise<{ email_exists: boolean; username_exists: boolean; has_conflict: boolean }> {
  const query = new URLSearchParams()
  if (params.email) query.set('email', params.email)
  if (params.username) query.set('username', params.username)
  return apiRequest(`/api/auth/check-duplicate?${query.toString()}`, { token: null })
}

export async function listAuthProviders(tenantId?: string | null): Promise<AuthProvider[]> {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return apiRequest<AuthProvider[]>(`/api/auth/providers${query}`, { token: null })
}

export async function authorizeProvider(
  provider: string,
  redirectUri: string,
  state: string,
): Promise<{ authorization_url: string }> {
  const query = new URLSearchParams({ redirect_uri: redirectUri, state })
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/authorize?${query.toString()}`, {
    token: null,
  })
}

export async function oauthCallback(
  provider: string,
  body: { code?: string; pending_token?: string; tenant_id?: string },
): Promise<TokenResponse | MultiTenantResponse> {
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/callback`, {
    method: 'POST',
    body,
    token: null,
  })
}

export async function bindProvider(provider: string, code: string): Promise<UserOut> {
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/bind`, {
    method: 'POST',
    body: { provider_type: provider, code },
  })
}

export async function unbindProvider(provider: string): Promise<UserOut> {
  return apiRequest(`/api/auth/${encodeURIComponent(provider)}/unbind`, {
    method: 'POST',
    body: { provider_type: provider },
  })
}

export async function fetchMyTenants(): Promise<TenantChoice[]> {
  return apiRequest<TenantChoice[]>('/api/auth/my-tenants')
}

export async function switchTenant(tenantId: string): Promise<TenantSwitchResponse> {
  return apiRequest<TenantSwitchResponse>('/api/auth/switch-tenant', {
    method: 'POST',
    body: { tenant_id: tenantId },
  })
}

export async function fetchMyTenant(): Promise<{
  id: string
  name: string
  slug: string
}> {
  return apiRequest('/api/tenants/me')
}

export type { SuggestedOrg }
