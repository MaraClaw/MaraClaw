export const TOKEN_KEY = 'maraclaw-admin-token'

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

type StorageNotice = Pick<StorageEvent, 'key' | 'storageArea'>
type StorageTarget = {
  readonly localStorage: Storage
  readonly addEventListener: (type: 'storage', listener: (event: StorageNotice) => void) => void
  readonly removeEventListener: (type: 'storage', listener: (event: StorageNotice) => void) => void
}

export function listenForTokenChanges(target: StorageTarget, reconcile: () => void) {
  const onStorage = (event: StorageNotice) => {
    if (event.storageArea !== target.localStorage) return
    if (event.key === TOKEN_KEY || event.key === null) reconcile()
  }
  target.addEventListener('storage', onStorage)
  return () => target.removeEventListener('storage', onStorage)
}
