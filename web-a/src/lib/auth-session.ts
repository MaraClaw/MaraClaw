import { ApiError } from './http.ts'
import { StaleSessionError, runOwned, type SessionOwner, type SessionPolicy } from './session-policy.ts'
import type { LoginRequest, MultiTenantResponse, TokenResponse, UserOut } from './types/auth.ts'
import { isAdminUser, isMultiTenantResponse, isTokenResponse } from './types/auth-guards.ts'

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
/** Runs `retry` after `delayMs`; returns the cancel function. */
export type ScheduleRetry = (retry: () => void, delayMs: number) => () => void

const FIRST_RETRY_DELAY_MS = 2_000
const MAX_RETRY_DELAY_MS = 30_000
const scheduleTimer: ScheduleRetry = (retry, delayMs) => {
  const timer = setTimeout(retry, delayMs)
  return () => { clearTimeout(timer) }
}

export function createAuthSession(policy: SessionPolicy, api: AuthApi, schedule = scheduleTimer) {
  const initialOwner = policy.capture()
  let snapshot: AuthSnapshot = {
    owner: initialOwner, status: initialOwner.token ? 'loading' : 'anonymous', user: null,
  }
  const listeners = new Set<() => void>()
  const loginOwners = new WeakMap<LoginResult, SessionOwner>()
  let refreshController: AbortController | undefined
  let loginController: AbortController | undefined
  let loginAttempt = 0
  let cancelRetry: (() => void) | null = null
  let retries = 0

  function stopRetry() {
    cancelRetry?.()
    cancelRetry = null
  }

  function publish(next: AuthSnapshot) {
    snapshot = next
    for (const listener of listeners) listener()
  }

  function reset(owner: SessionOwner) {
    publish({ owner, status: owner.token ? 'loading' : 'anonymous', user: null })
  }

  async function refreshUser(): Promise<SessionOwner | null> {
    const owner = policy.capture()
    stopRetry()
    refreshController?.abort()
    const controller = new AbortController()
    refreshController = controller
    if (!owner.token) {
      reset(owner)
      return null
    }
    const signal = AbortSignal.any([owner.signal, controller.signal])
    const token = owner.token
    try {
      const me = await runOwned(policy, owner, () => api.fetchCurrentUser(token, signal))
      if (signal.aborted) return null
      retries = 0
      if (!isAdminUser(me)) {
        policy.expire(owner)
        return null
      }
      publish({ owner, status: 'authenticated', user: me })
      return owner
    } catch (error) {
      if (error instanceof StaleSessionError || signal.aborted || !policy.isCurrent(owner)) return null
      // Only a 401 disproves the token. Any other failure keeps it: a validated admin stays signed in, and an unvalidated
      // session stays loading, never authenticated without the server's answer, and is checked again with backoff.
      if (error instanceof ApiError && error.status === 401) {
        retries = 0
        policy.expire(owner)
        return null
      }
      const validated = snapshot.owner.generation === owner.generation && snapshot.status === 'authenticated'
      if (!validated) {
        const delayMs = Math.min(MAX_RETRY_DELAY_MS, FIRST_RETRY_DELAY_MS * 2 ** retries)
        retries += 1
        cancelRetry = schedule(() => {
          cancelRetry = null
          if (policy.isCurrent(owner)) void refreshUser()
        }, delayMs)
      }
      return null
    }
  }

  async function refreshReplacement(expectedOwner: SessionOwner, token: string | undefined): Promise<SessionOwner | null> {
    if (!policy.isCurrent(expectedOwner)) return null
    if (!token) {
      policy.expire(expectedOwner)
      return null
    }
    policy.replace(token)
    return refreshUser()
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
        stopRetry()
        reset(owner)
        if (reason === 'storage' && owner.token) void refreshUser()
      })
      void refreshUser()
      return () => {
        unsubscribe()
        stopRetry()
        refreshController?.abort()
        loginController?.abort()
      }
    },
    login,
    applySession,
    refreshUser,
    refreshReplacement,
    isCurrent: policy.isCurrent,
    logout: () => { policy.logout() },
    isLoginCurrent(result: LoginResult) {
      const owner = loginOwners.get(result)
      return owner !== undefined && policy.isCurrent(owner)
    },
  }
}
