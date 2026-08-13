import { apiRequest } from '@/lib/http'
import type { LoginRequest, MultiTenantResponse, TokenResponse, UserOut } from '@/lib/types/auth'

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

export async function fetchCurrentUser(token: string, signal?: AbortSignal): Promise<UserOut> {
  return apiRequest<UserOut>('/api/auth/me', {
    method: 'GET',
    token,
    signal,
  })
}
