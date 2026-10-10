import { ApiError } from './http.ts'
import { StaleSessionError, runOwned, type SessionOwner, type SessionPolicy } from './session-policy.ts'
import {
  isAdminUser,
  isMultiTenantResponse,
  isTokenResponse,
  type LoginRequest,
  type MultiTenantResponse,
  type TokenResponse,
  type UserOut,
} from './types/auth.ts'

type LoginResult = TokenResponse | MultiTenantResponse
type AuthApi = {
  readonly fetchCurrentUser: (token: string, signal: AbortSignal) => Promise<UserOut>
  readonly loginRequest: (input: LoginRequest, signal: AbortSignal) => Promise<LoginResult>
}
export type AuthSnapshot = {
  readonly owner: SessionOwner
  readonly status: 'loading' | 'authenticated' | 'anonymous'
  readonly user: UserOut | null
}

export function createAuthSession(policy: SessionPolicy, api: AuthApi) {
  const initialOwner = policy.capture()
  let snapshot: AuthSnapshot = {
    owner: initialOwner, status: initialOwner.token ? 'loading' : 'anonymous', user: null,
  }
  const listeners = new Set<() => void>()
  const loginOwners = new WeakMap<LoginResult, SessionOwner>()
  let refreshController: AbortController | undefined
  let loginController: AbortController | undefined
  let loginAttempt = 0

  function publish(next: AuthSnapshot) {
    snapshot = next
    for (const listener of listeners) listener()
  }

  function reset(owner: SessionOwner) {
    publish({ owner, status: owner.token ? 'loading' : 'anonymous', user: null })
  }

  async function refreshUser() {
    const owner = policy.capture()
    refreshController?.abort()
    const controller = new AbortController()
    refreshController = controller
    if (!owner.token) {
      reset(owner)
      return
    }
    const signal = AbortSignal.any([owner.signal, controller.signal])
    const token = owner.token
    try {
      const me = await runOwned(policy, owner, () => api.fetchCurrentUser(token, signal))
      if (signal.aborted) return
      if (!isAdminUser(me)) {
        policy.expire(owner)
        return
      }
      publish({ owner, status: 'authenticated', user: me })
    } catch (error) {
      if (error instanceof StaleSessionError || signal.aborted || !policy.isCurrent(owner)) return
      policy.expire(owner)
    }
  }

  function applySession(session: TokenResponse) {
    if (!isAdminUser(session.user)) {
      policy.expire(policy.capture())
      throw new ApiError(
        403, 'Admin access required. Sign in with a platform admin or organization admin account.',
      )
    }
    const user: UserOut = {
      ...session.user,
      must_change_password: session.must_change_password === true ||
        session.user.must_change_password === true || session.identity?.must_change_password === true,
    }
    const owner = policy.replace(session.access_token)
    publish({ owner, status: 'authenticated', user })
  }

  async function login(input: LoginRequest): Promise<LoginResult> {
    const owner = policy.capture()
    const attempt = ++loginAttempt
    loginController?.abort()
    const controller = new AbortController()
    loginController = controller
    const signal = AbortSignal.any([owner.signal, controller.signal])
    let result: LoginResult
    try {
      result = await runOwned(policy, owner, () => api.loginRequest(input, signal))
    } catch (error) {
      if (signal.aborted || attempt !== loginAttempt) throw new StaleSessionError()
      throw error
    }
    if (signal.aborted || attempt !== loginAttempt) throw new StaleSessionError()
    if (isTokenResponse(result)) {
      applySession(result)
    } else if (!isMultiTenantResponse(result)) {
      throw new ApiError(500, 'Unexpected login response')
    }
    loginOwners.set(result, snapshot.owner)
    return result
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    connect() {
      reset(policy.capture())
      const unsubscribe = policy.subscribe((owner, reason) => {
        reset(owner)
        if (reason === 'storage' && owner.token) void refreshUser()
      })
      void refreshUser()
      return () => {
        unsubscribe()
        refreshController?.abort()
        loginController?.abort()
      }
    },
    login,
    applySession,
    refreshUser,
    logout: () => { policy.logout() },
    isLoginCurrent(result: LoginResult) {
      const owner = loginOwners.get(result)
      return owner !== undefined && policy.isCurrent(owner)
    },
  }
}
