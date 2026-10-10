import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { createSessionPolicy } from '../src/lib/session-policy.ts'
import type { TokenResponse, UserOut } from '../src/lib/types/auth.ts'

export class MemoryStorage implements Storage {
  readonly #values = new Map<string, string>()

  get length() { return this.#values.size }
  clear() { this.#values.clear() }
  getItem(key: string) { return this.#values.get(key) ?? null }
  key(index: number) { return [...this.#values.keys()][index] ?? null }
  removeItem(key: string) { this.#values.delete(key) }
  setItem(key: string, value: string) { this.#values.set(key, value) }
}

export function createFixture(token: string | null = 'token-a') {
  const storage = new MemoryStorage()
  if (token !== null) storage.setItem(TOKEN_KEY, token)
  const policy = createSessionPolicy({
    read: () => storage.getItem(TOKEN_KEY),
    write: (next) => storage.setItem(TOKEN_KEY, next),
    clear: () => storage.removeItem(TOKEN_KEY),
  })
  return { storage, policy }
}

export function adminUser(id = 'admin-a'): UserOut {
  return {
    id, identity_id: `identity-${id}`, username: id, email: `${id}@example.test`,
    display_name: id, avatar_url: null, role: 'org_admin', is_platform_admin: false,
    tenant_id: `tenant-${id}`, title: null, primary_mobile: null,
    registration_source: null, is_active: true, email_verified: true,
    must_change_password: false, created_at: '2026-10-09T00:00:00Z',
  }
}

export function tokenResponse(token = 'token-b'): TokenResponse {
  return { access_token: token, token_type: 'bearer', user: adminUser('admin-b') }
}

type StorageNotice = Pick<StorageEvent, 'key' | 'storageArea'> & {
  readonly newValue?: string | null
}

export function storageTarget(localStorage: Storage) {
  const listeners = new Set<(event: StorageNotice) => void>()
  return {
    localStorage,
    addEventListener(_type: 'storage', listener: (event: StorageNotice) => void) {
      listeners.add(listener)
    },
    removeEventListener(_type: 'storage', listener: (event: StorageNotice) => void) {
      listeners.delete(listener)
    },
    emit(event: StorageNotice) { for (const listener of listeners) listener(event) },
    get listenerCount() { return listeners.size },
  }
}

export class DeferredBodyResponse extends Response {
  readonly #body: Promise<string>
  readonly bodyStarted = Promise.withResolvers<void>()

  constructor(status: number, body: Promise<string>) {
    super(null, { status })
    this.#body = body
  }

  override text() {
    this.bodyStarted.resolve()
    return this.#body
  }
}
