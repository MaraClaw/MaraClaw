import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { fetchCurrentUser, loginRequest } from '@/lib/auth-api'
import { clearStoredToken, getStoredToken, setStoredToken } from '@/lib/auth-storage'
import { ApiError } from '@/lib/http'
import {
  isAdminUser,
  isMultiTenantResponse,
  isTokenResponse,
  userMustChangePassword,
  type LoginRequest,
  type MultiTenantResponse,
  type TokenResponse,
  type UserOut,
} from '@/lib/types/auth'

type AuthStatus = 'loading' | 'authenticated' | 'anonymous'

type AuthContextValue = {
  status: AuthStatus
  user: UserOut | null
  token: string | null
  isAdmin: boolean
  mustChangePassword: boolean
  login: (input: LoginRequest) => Promise<TokenResponse | MultiTenantResponse>
  applySession: (session: TokenResponse) => void
  logout: () => void
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [user, setUser] = useState<UserOut | null>(null)
  const [token, setToken] = useState<string | null>(() => getStoredToken())

  const clearSession = useCallback(() => {
    clearStoredToken()
    setToken(null)
    setUser(null)
    setStatus('anonymous')
  }, [])

  const applySession = useCallback((session: TokenResponse) => {
    if (!isAdminUser(session.user)) {
      clearStoredToken()
      setToken(null)
      setUser(null)
      setStatus('anonymous')
      throw new ApiError(
        403,
        'Admin access required. Sign in with a platform admin or organization admin account.',
      )
    }
    const nextUser: UserOut = {
      ...session.user,
      must_change_password:
        session.must_change_password === true ||
        session.user.must_change_password === true ||
        session.identity?.must_change_password === true,
    }
    setStoredToken(session.access_token)
    setToken(session.access_token)
    setUser(nextUser)
    setStatus('authenticated')
  }, [])

  const refreshUser = useCallback(async () => {
    const stored = getStoredToken()
    if (!stored) {
      clearSession()
      return
    }
    try {
      const me = await fetchCurrentUser(stored)
      if (!isAdminUser(me)) {
        clearSession()
        return
      }
      setToken(stored)
      setUser(me)
      setStatus('authenticated')
    } catch {
      clearSession()
    }
  }, [clearSession])

  useEffect(() => {
    void refreshUser()
  }, [refreshUser])

  const login = useCallback(
    async (input: LoginRequest) => {
      const result = await loginRequest(input)
      if (isTokenResponse(result)) {
        applySession(result)
        return result
      }
      if (isMultiTenantResponse(result)) {
        return result
      }
      throw new ApiError(500, 'Unexpected login response')
    },
    [applySession],
  )

  const logout = useCallback(() => {
    clearSession()
  }, [clearSession])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      token,
      isAdmin: isAdminUser(user),
      mustChangePassword: userMustChangePassword(user),
      login,
      applySession,
      logout,
      refreshUser,
    }),
    [status, user, token, login, applySession, logout, refreshUser],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return ctx
}
