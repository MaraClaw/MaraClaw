import { z } from 'zod'
import { apiRequest } from './http.ts'
import {
  AuthUserSchema, ChangePasswordResponseSchema,
  ForgotPasswordResponseSchema, ResetPasswordResponseSchema,
} from './engine-contracts.generated.ts'
import { adminMultiTenantResponseSchema, adminTokenResponseSchema } from './types/auth.ts'
import type { LoginRequest, MultiTenantResponse, TokenResponse } from './types/auth.ts'
import type { AuthUser, ChangePasswordResponse, ForgotPasswordResponse, ResetPasswordResponse } from './engine-contracts.generated.ts'

export type { ChangePasswordResponse } from './engine-contracts.generated.ts'
const loginResponseSchema = z.union([adminTokenResponseSchema, adminMultiTenantResponseSchema])

export async function loginRequest(
  body: LoginRequest,
  signal?: AbortSignal,
): Promise<TokenResponse | MultiTenantResponse> {
  return apiRequest('/api/auth/login', {
    schema: loginResponseSchema,
    method: 'POST',
    body,
    token: null,
    signal,
  })
}

export async function fetchCurrentUser(token: string, signal?: AbortSignal): Promise<AuthUser> {
  return apiRequest('/api/auth/me', {
    schema: AuthUserSchema,
    method: 'GET',
    token,
    signal,
  })
}

export async function forgotPasswordRequest(
  email: string,
  signal?: AbortSignal,
): Promise<ForgotPasswordResponse> {
  return apiRequest('/api/auth/forgot-password', {
    schema: ForgotPasswordResponseSchema,
    method: 'POST',
    body: { email },
    token: null,
    signal,
  })
}

export async function resetPasswordRequest(
  body: { token: string; new_password: string },
  signal?: AbortSignal,
): Promise<ResetPasswordResponse> {
  return apiRequest('/api/auth/reset-password', {
    schema: ResetPasswordResponseSchema,
    method: 'POST',
    body,
    token: null,
    signal,
  })
}

export async function changePasswordRequest(
  body: { old_password: string; new_password: string },
  signal?: AbortSignal,
): Promise<ChangePasswordResponse> {
  return apiRequest('/api/auth/me/password', {
    schema: ChangePasswordResponseSchema,
    method: 'PUT',
    body,
    signal,
  })
}
