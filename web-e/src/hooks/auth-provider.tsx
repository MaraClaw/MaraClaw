import { useCallback, useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react'

import { AuthContext, type AuthContextValue } from '@/hooks/use-auth'
import { authSession, type SessionOwner } from '@/lib/auth-session'
import { createAuthSessionController } from '@/lib/auth-session-controller'
import { getStoredToken } from '@/lib/auth-storage'
import { ApiError, StaleSessionError } from '@/lib/http'
import { retryingImport } from '@/lib/retrying-import'
import type { LoginRequest } from '@/lib/types/auth'
import { isMultiTenantResponse, isTokenResponse } from '@/lib/types/auth-guards'

// The auth client and its response schemas (zod, generated contracts) stay out of the entry chunk: they download when a
// stored session is checked or a member signs in. A failed download fails that check like a failed request.
const loadAuthApi = retryingImport(() => import('@/lib/auth-api'))

export function AuthProvider({ children }: { children: ReactNode }) {
  const snapshot = useSyncExternalStore(authSession.subscribe, authSession.getSnapshot, authSession.getSnapshot)
  const controller = useMemo(() => createAuthSessionController(authSession, async (token, owner, signal) =>
    (await loadAuthApi()).fetchCurrentUser(token, signal, owner)), [])

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
    const { loginRequest } = await loadAuthApi()
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
