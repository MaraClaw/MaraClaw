import { clearStoredToken, getStoredToken, setStoredToken } from './auth-storage.ts'
import { sessionMustChangePassword } from './types/auth.ts'
import type { TokenResponse, UserOut } from './types/auth.ts'

const ownerBrand: unique symbol = Symbol('session owner')
export type SessionOwner = Readonly<{ [ownerBrand]: symbol }>
export type AuthSnapshot = Readonly<{ owner: SessionOwner; mustChangePassword: boolean } & (
  | { status: 'anonymous'; token: null; user: null }
  | { status: 'loading'; token: string; user: null }
  | { status: 'authenticated'; token: string; user: Readonly<UserOut> | null }
)>
export type SessionStorage = {
  readonly read: () => string | null
  readonly write: (token: string) => void
  readonly clear: () => void
}
export type RefreshAttempt = Readonly<{
  owner: SessionOwner
  token: string
  id: symbol
}>

function newOwner(): SessionOwner {
  return Object.freeze({ [ownerBrand]: Symbol() })
}

function pendingSnapshot(token: string | null): AuthSnapshot {
  return token
    ? Object.freeze({ owner: newOwner(), status: 'loading', token, user: null, mustChangePassword: false })
    : Object.freeze({ owner: newOwner(), status: 'anonymous', token: null, user: null, mustChangePassword: false })
}

/** All async callers must capture an owner before starting work. */
export function createAuthSessionStore(storage: SessionStorage) {
  let snapshot = pendingSnapshot(storage.read())
  let attempt: RefreshAttempt | null = null
  const listeners = new Set<() => void>()
  const isCurrent = (owner: SessionOwner): boolean =>
    snapshot.owner === owner && storage.read() === snapshot.token
  const publish = (next: AuthSnapshot): void => {
    snapshot = next
    attempt = null
    for (const listener of listeners) listener()
  }
  const expire = (owner: SessionOwner): boolean => {
    if (!isCurrent(owner)) return false
    storage.clear()
    publish(pendingSnapshot(null))
    return true
  }
  return {
    getSnapshot: (): AuthSnapshot => snapshot,
    capture: (): SessionOwner => snapshot.owner,
    isCurrent,
    subscribe(listener: () => void): () => void {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    applySession(owner: SessionOwner, session: TokenResponse): boolean {
      if (!isCurrent(owner)) return false
      const mustChangePassword = sessionMustChangePassword(session)
      storage.write(session.access_token)
      publish(Object.freeze({
        owner: newOwner(), status: 'authenticated', token: session.access_token,
        mustChangePassword,
        user: session.user === null ? null : Object.freeze({ ...session.user, must_change_password: mustChangePassword }),
      }))
      return true
    },
    beginReplacement(owner: SessionOwner, token: string): SessionOwner | null {
      if (!isCurrent(owner) || !token) return null
      storage.write(token)
      publish(pendingSnapshot(token))
      return snapshot.owner
    },
    /** Ignore event.newValue: a newer storage event may already have overtaken it. */
    adoptStoredSession(owner: SessionOwner): SessionOwner | null {
      if (snapshot.owner !== owner) return null
      publish(pendingSnapshot(storage.read()))
      return snapshot.owner
    },
    beginRefresh(owner: SessionOwner): RefreshAttempt | null {
      if (!isCurrent(owner) || !snapshot.token) return null
      attempt = Object.freeze({ owner, token: snapshot.token, id: Symbol() })
      return attempt
    },
    commitRefresh(candidate: RefreshAttempt, user: UserOut): boolean {
      if (attempt !== candidate || !isCurrent(candidate.owner)) return false
      publish(Object.freeze({
        owner: candidate.owner, status: 'authenticated', token: candidate.token,
        mustChangePassword: user.must_change_password === true,
        user: Object.freeze({ ...user }),
      }))
      return true
    },
    failRefresh(candidate: RefreshAttempt, failure?: number | 'member_audience_rejected'): boolean {
      if (attempt !== candidate || !isCurrent(candidate.owner)) return false
      attempt = null
      if (failure === 401 || failure === 'member_audience_rejected' || snapshot.status === 'loading') return expire(candidate.owner)
      // Transport, 5xx, and permission failures do not invalidate a validated identity.
      return false
    },
    expire,
    logout: expire,
  }
}

export type AuthSessionStore = ReturnType<typeof createAuthSessionStore>
export const authSession = createAuthSessionStore({
  read: getStoredToken, write: setStoredToken, clear: clearStoredToken,
})
