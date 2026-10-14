import type { AuthSessionStore, SessionOwner } from './auth-session.ts'
import { subscribeStoredToken } from './auth-storage.ts'
import type { StorageEventSource } from './auth-storage.ts'
import type { UserOut } from './types/auth.ts'

export class InvalidReplacementTokenError extends Error {
  constructor() {
    super('Unexpected organization response')
    this.name = 'InvalidReplacementTokenError'
  }
}

export type FetchSessionUser = (token: string, owner: SessionOwner, signal: AbortSignal) => Promise<UserOut>

/** HTTP is injected so the store never imports its own HTTP consumer. */
export function createAuthSessionController(store: AuthSessionStore, fetchUser: FetchSessionUser) {
  let active: AbortController | null = null
  const cancelRefresh = (): void => {
    active?.abort()
    active = null
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
      return !request.signal.aborted && store.commitRefresh(attempt, user)
    } catch (error) {
      if (request.signal.aborted || !store.isCurrent(attempt.owner)) return false
      // ApiError is deliberately recognized structurally to avoid an HTTP import cycle.
      if (error instanceof Error && 'status' in error && typeof error.status === 'number') {
        store.failRefresh(attempt, error.status)
        return false
      }
      if (error instanceof TypeError || (error instanceof Error && (error.name === 'AbortError' || error.name === 'StaleSessionError'))) {
        store.failRefresh(attempt)
        return false
      }
      store.failRefresh(attempt)
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
