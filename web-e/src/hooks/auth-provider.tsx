import { useCallback, useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react'

import { AuthContext, type AuthContextValue } from '@/hooks/use-auth'
import { fetchCurrentUser, loginRequest } from '@/lib/auth-api'
import { authSession, type SessionOwner } from '@/lib/auth-session'
import { createAuthSessionController } from '@/lib/auth-session-controller'
import { getStoredToken } from '@/lib/auth-storage'
import { ApiError, StaleSessionError } from '@/lib/http'
import { isMultiTenantResponse, isTokenResponse } from '@/lib/types/auth'
import type { LoginRequest } from '@/lib/types/auth'

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
    if (isTokenResponse(result) || isMultiTenantResponse(result)) return result
    throw new ApiError(500, 'Unexpected login response')
  }, [])

  const logout = useCallback(() => { authSession.logout(snapshot.owner) }, [snapshot.owner])
  const value = useMemo<AuthContextValue>(() => ({
    ...snapshot,
    needsOrgConfirm: snapshot.status === 'authenticated' && snapshot.user?.tenant_id == null,
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
