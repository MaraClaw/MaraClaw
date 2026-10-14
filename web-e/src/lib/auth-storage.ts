const TOKEN_KEY = 'maraclaw-enduser-token'

type TokenStorageEvent = Pick<StorageEvent, 'key' | 'storageArea'>
export type StorageEventSource = {
  readonly localStorage: Storage
  readonly addEventListener: (type: 'storage', listener: (event: TokenStorageEvent) => void) => void
  readonly removeEventListener: (type: 'storage', listener: (event: TokenStorageEvent) => void) => void
}

/** Storage events announce a change; callers read the latest value from storage. */
export function subscribeStoredToken(listener: () => void, source: StorageEventSource = window): () => void {
  const onStorage = (event: TokenStorageEvent): void => {
    if (event.storageArea !== source.localStorage || (event.key !== TOKEN_KEY && event.key !== null)) return
    listener()
  }
  source.addEventListener('storage', onStorage)
  return () => source.removeEventListener('storage', onStorage)
}

export function getStoredToken(): string | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setStoredToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token)
}

export function clearStoredToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY)
  } catch {
    // ignore storage failures
  }
}
