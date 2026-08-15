import { apiRequest } from '@/lib/http'
import type {
  EmailLookupResponse,
  LoginRequest,
  MultiTenantResponse,
  SuggestedOrg,
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

export type { SuggestedOrg }
