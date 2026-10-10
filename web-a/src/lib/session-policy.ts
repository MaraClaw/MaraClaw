export type SessionOwner = {
  readonly generation: number
  readonly token: string | null
  readonly signal: AbortSignal
}

export type SessionReason = 'login' | 'logout' | 'storage' | 'expired'
type SessionListener = (owner: SessionOwner, reason: SessionReason) => void

export type TokenStorage = {
  readonly read: () => string | null
  readonly write: (token: string) => void
  readonly clear: () => void
}

export class StaleSessionError extends Error {
  constructor() {
    super('This operation belongs to a retired admin session')
    this.name = 'StaleSessionError'
  }
}

export function createSessionPolicy(storage: TokenStorage) {
  let controller = new AbortController()
  let owner: SessionOwner = { generation: 0, token: storage.read(), signal: controller.signal }
  const listeners = new Set<SessionListener>()

  function transition(token: string | null, reason: SessionReason) {
    const previousController = controller
    controller = new AbortController()
    owner = { generation: owner.generation + 1, token, signal: controller.signal }
    previousController.abort(new StaleSessionError())
    for (const listener of listeners) listener(owner, reason)
    return owner
  }

  function reconcile() {
    const stored = storage.read()
    if (stored !== owner.token) transition(stored, 'storage')
    return owner
  }

  function isCurrent(candidate: SessionOwner) {
    return candidate.generation === owner.generation && candidate.token === owner.token &&
      candidate.token === storage.read() && !candidate.signal.aborted
  }

  function assertCurrent(candidate: SessionOwner) {
    if (!isCurrent(candidate)) throw new StaleSessionError()
  }

  return {
    capture: reconcile,
    reconcile,
    isCurrent,
    assertCurrent,
    subscribe(listener: SessionListener) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    replace(token: string) {
      storage.write(token)
      return transition(token, 'login')
    },
    logout() {
      storage.clear()
      // Also fences an in-flight login when the tab is already anonymous.
      return transition(null, 'logout')
    },
    expire(candidate: SessionOwner) {
      if (!candidate.token || !isCurrent(candidate)) return false
      storage.clear()
      transition(null, 'expired')
      return true
    },
  }
}

export type SessionPolicy = ReturnType<typeof createSessionPolicy>

/** Fence both outcomes, even when a transport ignores cancellation. */
export async function runOwned<T>(policy: SessionPolicy, owner: SessionOwner, work: () => Promise<T>) {
  policy.assertCurrent(owner)
  try {
    const result = await work()
    policy.assertCurrent(owner)
    return result
  } catch (error) {
    policy.assertCurrent(owner)
    throw error
  }
}
