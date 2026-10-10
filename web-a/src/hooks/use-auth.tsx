import { QueryClientProvider } from '@tanstack/react-query'
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
import { fetchCurrentUser, loginRequest } from '@/lib/auth-api'
import { listenForTokenChanges } from '@/lib/auth-storage'
import { createAuthSession, type AuthSnapshot } from '@/lib/auth-session'
import { isAdminUser, userMustChangePassword } from '@/lib/types/auth'
import { createSessionQueryScope, ownQueryClient } from './session-query-client'

type AuthContextValue = ReturnType<typeof createAuthSession> & AuthSnapshot & {
  readonly token: string | null
  readonly isAdmin: boolean
  readonly mustChangePassword: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [auth] = useState(() => createAuthSession(adminSession, { fetchCurrentUser, loginRequest }))
  const snapshot = useSyncExternalStore(auth.subscribe, auth.getSnapshot, auth.getSnapshot)
  const { client: queryClient } = useMemo(
    () => createSessionQueryScope(snapshot.owner), [snapshot.owner],
  )

  useEffect(() => {
    const disconnect = auth.connect()
    const unlisten = listenForTokenChanges(window, () => { adminSession.reconcile() })
    return () => { unlisten(); disconnect() }
  }, [auth])

  useEffect(
    () => ownQueryClient(adminSession, snapshot.owner, queryClient),
    [snapshot.owner, queryClient],
  )

  const value = useMemo<AuthContextValue>(() => ({
    ...auth,
    ...snapshot,
    token: snapshot.owner.token,
    isAdmin: isAdminUser(snapshot.user),
    mustChangePassword: userMustChangePassword(snapshot.user),
  }), [auth, snapshot])

  return (
    <AuthContext.Provider value={value}>
      <QueryClientProvider key={snapshot.owner.generation} client={queryClient}>
        {children}
      </QueryClientProvider>
    </AuthContext.Provider>
  )
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
