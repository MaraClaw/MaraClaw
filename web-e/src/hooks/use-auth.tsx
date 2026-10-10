import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { fetchCurrentUser, loginRequest } from '@/lib/auth-api'
import { clearStoredToken, getStoredToken, setStoredToken } from '@/lib/auth-storage'
import { ApiError } from '@/lib/http'
import {
  isMultiTenantResponse,
  isPlatformAdminUser,
  isTokenResponse,
  sessionMustChangePassword,
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
  needsOrgConfirm: boolean
  mustChangePassword: boolean
  login: (input: LoginRequest) => Promise<TokenResponse | MultiTenantResponse>
  applySession: (session: TokenResponse) => void
  logout: () => void
  refreshUser: (replacementToken?: string | null) => Promise<boolean>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [user, setUser] = useState<UserOut | null>(null)
  const [token, setToken] = useState<string | null>(() => getStoredToken())
  const refreshRequest = useRef<AbortController | null>(null)

  const cancelRefresh = useCallback(() => {
    refreshRequest.current?.abort()
    refreshRequest.current = null
  }, [])

  const clearSession = useCallback(() => {
    cancelRefresh()
    clearStoredToken()
    setToken(null)
    setUser(null)
    setStatus('anonymous')
  }, [cancelRefresh])

  const applySession = useCallback((session: TokenResponse) => {
    cancelRefresh()
    if (isPlatformAdminUser(session.user)) {
      clearStoredToken()
      setToken(null)
      setUser(null)
      setStatus('anonymous')
      throw new ApiError(
        403,
        'Platform operators sign in on the admin console, not this member site.',
      )
    }
    const nextUser: UserOut = {
      ...session.user,
      must_change_password: sessionMustChangePassword(session),
    }
    setStoredToken(session.access_token)
    setToken(session.access_token)
    setUser(nextUser)
    setStatus('authenticated')
  }, [cancelRefresh])

  const refreshUser = useCallback(async (replacementToken?: string | null) => {
    if (replacementToken === null || replacementToken === '') {
      throw new ApiError(500, 'Unexpected organization response')
    }
    cancelRefresh()
    const stored = replacementToken ?? getStoredToken()
    if (!stored) {
      clearSession()
      return false
    }
    const controller = new AbortController()
    refreshRequest.current = controller
    if (replacementToken !== undefined) {
      setStoredToken(stored)
      setToken(stored)
      setUser(null)
      setStatus('loading')
    }
    try {
      const me = await fetchCurrentUser(stored, controller.signal)
      if (controller.signal.aborted || refreshRequest.current !== controller || getStoredToken() !== stored) {
        return false
      }
      if (isPlatformAdminUser(me)) {
        clearSession()
        return false
      }
      setToken(stored)
      setUser(me)
      setStatus('authenticated')
      return true
    } catch {
      if (controller.signal.aborted || refreshRequest.current !== controller) return false
      const currentToken = getStoredToken()
      if (currentToken !== null && currentToken !== stored) return false
      clearSession()
      return false
    } finally {
      if (refreshRequest.current === controller) refreshRequest.current = null
    }
  }, [cancelRefresh, clearSession])

  useEffect(() => {
    void refreshUser()
    return cancelRefresh
  }, [cancelRefresh, refreshUser])

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
      needsOrgConfirm: Boolean(user && user.tenant_id == null),
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
