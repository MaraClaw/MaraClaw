import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react'

import { fetchCurrentUser, loginRequest } from '@/lib/auth-api'
import { authSession, type AuthSnapshot, type SessionOwner } from '@/lib/auth-session'
import { createAuthSessionController } from '@/lib/auth-session-controller'
import { getStoredToken } from '@/lib/auth-storage'
import { ApiError, StaleSessionError } from '@/lib/http'
import { isMultiTenantResponse, isPlatformAdminUser, isTokenResponse, userMustChangePassword } from '@/lib/types/auth'
import type { LoginRequest, MultiTenantResponse, TokenResponse } from '@/lib/types/auth'

type AuthContextValue = AuthSnapshot & {
  readonly needsOrgConfirm: boolean
  readonly mustChangePassword: boolean
  readonly capture: typeof authSession.capture
  readonly isCurrent: typeof authSession.isCurrent
  readonly login: (owner: SessionOwner, input: LoginRequest) => Promise<TokenResponse | MultiTenantResponse>
  readonly applySession: typeof authSession.applySession
  readonly logout: () => void
  readonly refreshUser: (owner: SessionOwner, replacementToken?: string | null) => Promise<boolean>
  readonly refreshReplacement: (owner: SessionOwner, token: string | null) => Promise<SessionOwner | null>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const snapshot = useSyncExternalStore(authSession.subscribe, authSession.getSnapshot, authSession.getSnapshot)
  const controller = useMemo(() => createAuthSessionController(authSession, (token, owner, signal) =>
    fetchCurrentUser(token, signal, owner)), [])

  useEffect(() => {
    const unsubscribe = controller.subscribeStorage()
    if (getStoredToken() !== authSession.getSnapshot().token) authSession.adoptStoredSession(authSession.capture())
    if (authSession.getSnapshot().status === 'loading') void controller.refreshUser(authSession.capture())
    return () => {
      unsubscribe()
      controller.cancelRefresh()
    }
  }, [controller])

  const login = useCallback(async (owner: SessionOwner, input: LoginRequest) => {
    if (!authSession.isCurrent(owner)) throw new StaleSessionError(owner)
    const result = await loginRequest(input, undefined, owner)
    if (!authSession.isCurrent(owner)) throw new StaleSessionError(owner)
    if (isTokenResponse(result) && isPlatformAdminUser(result.user)) {
      throw new ApiError(403, 'Platform operators sign in on the admin console, not this member site.')
    }
    if (isTokenResponse(result) || isMultiTenantResponse(result)) return result
    throw new ApiError(500, 'Unexpected login response')
  }, [])

  const logout = useCallback(() => { authSession.logout(snapshot.owner) }, [snapshot.owner])
  const value = useMemo<AuthContextValue>(() => ({
    ...snapshot,
    needsOrgConfirm: Boolean(snapshot.user && snapshot.user.tenant_id == null),
    mustChangePassword: userMustChangePassword(snapshot.user),
    capture: authSession.capture,
    isCurrent: authSession.isCurrent,
    login,
    applySession: authSession.applySession,
    logout,
    refreshUser: controller.refreshUser,
    refreshReplacement: controller.refreshReplacement,
  }), [snapshot, login, logout, controller])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
