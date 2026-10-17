import { createContext, useContext } from 'react'

import { authSession, type AuthSnapshot, type SessionOwner } from '@/lib/auth-session'
import type { LoginRequest, MultiTenantResponse, TokenResponse } from '@/lib/types/auth'

export type AuthContextValue = AuthSnapshot & {
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

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
