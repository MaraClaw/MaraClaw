import { apiRequest } from '@/lib/http'
import type { LoginRequest, MultiTenantResponse, TokenResponse, UserOut } from '@/lib/types/auth'

export type OkMessageResponse = {
  ok: boolean
  message?: string
}

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

export async function forgotPasswordRequest(
  email: string,
  signal?: AbortSignal,
): Promise<OkMessageResponse> {
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

export async function changePasswordRequest(
  body: { old_password: string; new_password: string },
  signal?: AbortSignal,
): Promise<OkMessageResponse> {
  return apiRequest<OkMessageResponse>('/api/auth/me/password', {
    method: 'PUT',
    body,
    signal,
  })
}
