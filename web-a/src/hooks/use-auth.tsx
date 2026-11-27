import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'

import { adminSession } from '@/lib/admin-session'
import { listenForTokenChanges } from '@/lib/auth-storage'
import { createAuthSession, type AuthSnapshot } from '@/lib/auth-session'
import { retryingImport } from '@/lib/retrying-import'
import { isAdminUser, userMustChangePassword } from '@/lib/types/auth-guards'

// The auth client and its response schemas (zod, generated contracts) stay out of the entry chunk: they download when a
// stored session is checked or an admin signs in. A failed download fails that check or sign-in like a failed request.
const loadAuthApi = retryingImport(() => import('@/lib/auth-api'))

type AuthContextValue = ReturnType<typeof createAuthSession> & AuthSnapshot & {
  readonly token: string | null
  readonly isAdmin: boolean
  readonly mustChangePassword: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [auth] = useState(() => createAuthSession(adminSession, {
    fetchCurrentUser: async (token, signal) => (await loadAuthApi()).fetchCurrentUser(token, signal),
    loginRequest: async (input, signal) => (await loadAuthApi()).loginRequest(input, signal),
  }))
  const snapshot = useSyncExternalStore(auth.subscribe, auth.getSnapshot, auth.getSnapshot)

  useEffect(() => {
    const disconnect = auth.connect()
    const unlisten = listenForTokenChanges(window, () => { adminSession.reconcile() })
    return () => { unlisten(); disconnect() }
  }, [auth])

  const value = useMemo<AuthContextValue>(() => ({
    ...auth,
    ...snapshot,
    token: snapshot.owner.token,
    isAdmin: isAdminUser(snapshot.user),
    mustChangePassword: userMustChangePassword(snapshot.user),
  }), [auth, snapshot])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

export function useSessionCurrent() {
  const { owner } = useAuth()
  return useCallback(() => adminSession.isCurrent(owner), [owner])
}
