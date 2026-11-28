import type { AuthSessionStore, SessionOwner } from './auth-session.ts'
import { subscribeStoredToken } from './auth-storage.ts'
import type { StorageEventSource } from './auth-storage.ts'
import type { UserOut } from './types/auth.ts'
import { ApiError, RequestTimeoutError } from './http.ts'

export class InvalidReplacementTokenError extends Error {
  constructor() {
    super('Unexpected organization response')
    this.name = 'InvalidReplacementTokenError'
  }
}

export type FetchSessionUser = (token: string, owner: SessionOwner, signal: AbortSignal) => Promise<UserOut>
/** Runs `retry` after `delayMs`; returns the cancel function. */
export type ScheduleRetry = (retry: () => void, delayMs: number) => () => void

const FIRST_RETRY_DELAY_MS = 2_000
const MAX_RETRY_DELAY_MS = 30_000
const scheduleTimer: ScheduleRetry = (retry, delayMs) => {
  const timer = setTimeout(retry, delayMs)
  return () => { clearTimeout(timer) }
}

function isTransient(error: unknown): boolean {
  return error instanceof TypeError || error instanceof RequestTimeoutError ||
    (error instanceof Error && (error.name === 'AbortError' || error.name === 'StaleSessionError'))
}

/** HTTP is injected so the store never imports its own HTTP consumer. */
export function createAuthSessionController(store: AuthSessionStore, fetchUser: FetchSessionUser, schedule = scheduleTimer) {
  let active: AbortController | null = null
  let cancelRetry: (() => void) | null = null
  let retries = 0
  const cancelRefresh = (): void => {
    active?.abort()
    active = null
    cancelRetry?.()
    cancelRetry = null
  }
  const refreshUser = async (owner: SessionOwner, replacementToken?: string | null): Promise<boolean> => {
    if (!store.isCurrent(owner)) return false
    if (replacementToken === null || replacementToken === '') {
      throw new InvalidReplacementTokenError()
    }
    cancelRefresh()
    const refreshOwner = replacementToken === undefined
      ? owner
      : store.beginReplacement(owner, replacementToken)
    if (!refreshOwner) return false
    const attempt = store.beginRefresh(refreshOwner)
    if (!attempt) return false
    const request = new AbortController()
    active = request
    try {
      const user = await fetchUser(attempt.token, attempt.owner, request.signal)
      if (request.signal.aborted) return false
      retries = 0
      return store.commitRefresh(attempt, user)
    } catch (error) {
      if (request.signal.aborted || !store.isCurrent(attempt.owner)) return false
      const status = error instanceof Error && 'status' in error && typeof error.status === 'number' ? error.status : undefined
      const expired = store.failRefresh(attempt, error instanceof ApiError && error.isMemberAudienceRejection ? 'member_audience_rejected' : status)
      if (expired) {
        retries = 0
      } else if (store.getSnapshot().status === 'loading' && store.isCurrent(attempt.owner)) {
        // The failure proved nothing about the token: it stays loading, never authenticated without the server's answer,
        // and this controller owns the failure by checking again.
        const delayMs = Math.min(MAX_RETRY_DELAY_MS, FIRST_RETRY_DELAY_MS * 2 ** retries)
        retries += 1
        cancelRetry = schedule(() => {
          cancelRetry = null
          void refreshUser(attempt.owner)
        }, delayMs)
        return false
      }
      if (status !== undefined || isTransient(error)) return false
      throw error
    } finally {
      if (active === request) active = null
    }
  }
  return {
    cancelRefresh,
    refreshUser,
    async refreshReplacement(owner: SessionOwner, token: string | null): Promise<SessionOwner | null> {
      const refresh = refreshUser(owner, token)
      // Replacement ownership is installed synchronously, before validation awaits.
      const installedOwner = store.capture()
      return await refresh ? installedOwner : null
    },
    subscribeStorage(source?: StorageEventSource): () => void {
      return subscribeStoredToken(() => {
        const owner = store.adoptStoredSession(store.capture())
        if (owner) void refreshUser(owner)
      }, source)
    },
  }
}
