import assert from 'node:assert/strict'
import { test } from 'node:test'
import { subscribeStoredToken } from '../src/lib/auth-storage.ts'
import type { StorageEventSource } from '../src/lib/auth-storage.ts'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createAuthSessionController } from '../src/lib/auth-session-controller.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

class MemoryStorage implements Storage {
  readonly values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, value) }
}

class FakeStorageEvent extends Event {
  readonly key: string | null
  readonly storageArea: Storage | null
  readonly newValue: string | null
  constructor(key: string | null, storageArea: Storage | null, newValue: string | null = null) {
    super('storage')
    this.key = key
    this.storageArea = storageArea
    this.newValue = newValue
  }
}

function eventSource() {
  const target = new EventTarget()
  const localStorage = new MemoryStorage()
  const listeners = new Map<(event: Pick<StorageEvent, 'key' | 'storageArea'>) => void, EventListener>()
  const source: StorageEventSource = {
    localStorage,
    addEventListener(type, listener) {
      const dispatch: EventListener = (event) => {
        if (event instanceof FakeStorageEvent) listener(event)
      }
      listeners.set(listener, dispatch)
      target.addEventListener(type, dispatch)
    },
    removeEventListener(type, listener) {
      const dispatch = listeners.get(listener)
      if (dispatch) target.removeEventListener(type, dispatch)
      listeners.delete(listener)
    },
  }
  return { source, target, localStorage, listeners }
}

const key = 'maraclaw-enduser-token'
const user: UserOut = {
  id: 'member', username: null, email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant', email_verified: true,
}

test('storage listener filters storage area and unrelated keys and unsubscribes', () => {
  // Given
  const { source, target, localStorage, listeners } = eventSource()
  let calls = 0
  const unsubscribe = subscribeStoredToken(() => { calls++ }, source)
  // When
  target.dispatchEvent(new FakeStorageEvent(key, new MemoryStorage()))
  target.dispatchEvent(new FakeStorageEvent('theme', localStorage))
  target.dispatchEvent(new FakeStorageEvent(key, localStorage))
  target.dispatchEvent(new FakeStorageEvent(null, localStorage))
  unsubscribe()
  target.dispatchEvent(new FakeStorageEvent(key, localStorage))
  // Then
  assert.equal(calls, 2)
  assert.equal(listeners.size, 0)
})

test('provider storage subscription adopts latest token for each accepted event without writeback', async (t) => {
  // Given
  const { source, target, localStorage } = eventSource()
  localStorage.setItem(key, 'original')
  let writes = 0
  const store = createAuthSessionStore({
    read: () => localStorage.getItem(key),
    write: (token) => { writes++; localStorage.setItem(key, token) },
    clear: () => { writes++; localStorage.removeItem(key) },
  })
  const pending = Promise.withResolvers<UserOut>()
  const validated = Promise.withResolvers<void>()
  let validations = 0
  const controller = createAuthSessionController(store, async (token) => {
    validations++
    assert.equal(token, 'latest')
    return pending.promise
  })
  t.after(store.subscribe(() => {
    if (store.getSnapshot().status === 'authenticated') validated.resolve()
  }))
  t.after(controller.subscribeStorage(source))
  // When
  localStorage.setItem(key, 'latest')
  target.dispatchEvent(new FakeStorageEvent(key, localStorage, 'obsolete'))
  target.dispatchEvent(new FakeStorageEvent(key, localStorage, 'latest'))
  pending.resolve(user)
  // Then
  await validated.promise
  assert.equal(validations, 2)
  assert.equal(store.getSnapshot().token, 'latest')
  assert.equal(writes, 0)
})

test('storage clear fences in-flight validation without writing back', async () => {
  // Given
  const { source, target, localStorage } = eventSource()
  localStorage.setItem(key, 'original')
  let writes = 0
  const store = createAuthSessionStore({
    read: () => localStorage.getItem(key),
    write: () => { writes++ },
    clear: () => { writes++ },
  })
  const pending = Promise.withResolvers<UserOut>()
  const controller = createAuthSessionController(store, () => pending.promise)
  const refresh = controller.refreshUser(store.capture())
  const unsubscribe = controller.subscribeStorage(source)
  // When
  localStorage.clear()
  target.dispatchEvent(new FakeStorageEvent(null, localStorage))
  pending.resolve(user)
  // Then
  assert.equal(await refresh, false)
  assert.equal(store.getSnapshot().status, 'anonymous')
  assert.equal(writes, 0)
  unsubscribe()
})

test('provider storage subscription fences pending work when a same-token event is accepted', async (t) => {
  // Given
  const { source, target, localStorage, listeners } = eventSource()
  localStorage.setItem(key, 'same-token')
  let writes = 0
  const store = createAuthSessionStore({
    read: () => localStorage.getItem(key),
    write: () => { writes++ },
    clear: () => { writes++ },
  })
  const pending = Promise.withResolvers<UserOut>()
  let validations = 0
  const controller = createAuthSessionController(store, () => {
    validations++
    return pending.promise
  })
  const expectedOwner = store.capture()
  const oldRefresh = controller.refreshUser(expectedOwner)
  const unsubscribe = controller.subscribeStorage(source)
  t.after(unsubscribe)
  // When
  target.dispatchEvent(new FakeStorageEvent(key, localStorage, 'same-token'))
  const installedOwner = store.capture()
  unsubscribe()
  target.dispatchEvent(new FakeStorageEvent(key, localStorage, 'same-token'))
  pending.resolve(user)
  // Then
  assert.notEqual(installedOwner, expectedOwner)
  assert.equal(store.isCurrent(expectedOwner), false)
  assert.equal(await oldRefresh, false)
  assert.equal(store.capture(), installedOwner)
  assert.equal(validations, 2)
  assert.equal(listeners.size, 0)
  assert.equal(writes, 0)
})

test('provider storage subscription gives each queued ABA event a fresh owner from latest storage', async (t) => {
  // Given
  const { source, target, localStorage } = eventSource()
  localStorage.setItem(key, 'token-a')
  let writes = 0
  const store = createAuthSessionStore({
    read: () => localStorage.getItem(key),
    write: () => { writes++ },
    clear: () => { writes++ },
  })
  const pending = Promise.withResolvers<UserOut>()
  const validated = Promise.withResolvers<void>()
  const tokens: string[] = []
  const controller = createAuthSessionController(store, (token) => {
    tokens.push(token)
    return pending.promise
  })
  t.after(store.subscribe(() => {
    if (store.getSnapshot().status === 'authenticated') validated.resolve()
  }))
  t.after(controller.subscribeStorage(source))
  const originalOwner = store.capture()
  localStorage.setItem(key, 'token-b')
  localStorage.setItem(key, 'token-a')
  // When
  target.dispatchEvent(new FakeStorageEvent(key, localStorage, 'token-b'))
  const firstOwner = store.capture()
  target.dispatchEvent(new FakeStorageEvent(key, localStorage, 'token-a'))
  const secondOwner = store.capture()
  pending.resolve(user)
  // Then
  assert.notEqual(firstOwner, originalOwner)
  assert.notEqual(secondOwner, firstOwner)
  assert.deepEqual(tokens, ['token-a', 'token-a'])
  await validated.promise
  assert.equal(store.isCurrent(originalOwner), false)
  assert.equal(store.isCurrent(firstOwner), false)
  assert.equal(store.capture(), secondOwner)
  assert.equal(store.getSnapshot().token, 'token-a')
  assert.equal(writes, 0)
})
