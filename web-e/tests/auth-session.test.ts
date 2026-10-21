import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createAuthSessionController } from '../src/lib/auth-session-controller.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}
function fixture() {
  const values = new Map<string, string>()
  const key = 'maraclaw-enduser-token'
  let writes = 0
  const storage = {
    read: () => values.get(key) ?? null,
    write: (token: string) => { writes++; values.set(key, token) },
    clear: () => { writes++; values.delete(key) },
  }
  const store = createAuthSessionStore(storage)
  store.applySession(store.capture(), { access_token: 'token-a', user })
  return { store, storage, values, key, writes: () => writes }
}
function deferredUser() {
  const { promise, resolve, reject } = Promise.withResolvers<UserOut>()
  return { promise, resolve, reject }
}

test('stale completion cannot replace a newer session or storage', async () => {
  // Given
  const { store, storage } = fixture()
  const pending = deferredUser()
  const controller = createAuthSessionController(store, () => pending.promise)
  const refresh = controller.refreshUser(store.capture())
  store.applySession(store.capture(), { access_token: 'token-b', user: { ...user, tenant_id: 'tenant-b' } })
  const current = store.getSnapshot()
  // When
  pending.resolve(user)
  // Then
  assert.equal(await refresh, false)
  assert.equal(store.getSnapshot(), current)
  assert.equal(storage.read(), 'token-b')
})

test('current-owner expiry clears storage and invalidates captured work', () => {
  // Given
  const { store, storage } = fixture()
  const owner = store.capture()
  // When
  const expired = store.expire(owner)
  // Then
  assert.equal(expired, true)
  assert.equal(store.getSnapshot().status, 'anonymous')
  assert.equal(storage.read(), null)
  assert.equal(store.isCurrent(owner), false)
})

test('same-token replacement immediately fences old expiry and session application', async () => {
  // Given
  const { store, storage, writes } = fixture()
  const owner = store.capture()
  const pending = deferredUser()
  const controller = createAuthSessionController(store, () => pending.promise)
  const refresh = controller.refreshUser(owner, 'token-a')
  const replacement = store.getSnapshot()
  const count = writes()
  // When
  const expired = store.expire(owner)
  const applied = store.applySession(owner, { access_token: 'stale', user })
  // Then
  assert.notEqual(replacement.owner, owner)
  assert.equal(replacement.status, 'loading')
  assert.equal(expired, false)
  assert.equal(applied, false)
  assert.equal(store.getSnapshot(), replacement)
  assert.equal(storage.read(), 'token-a')
  assert.equal(writes(), count)
  pending.resolve(user)
  assert.equal(await refresh, true)
  assert.equal(store.capture(), replacement.owner)
})

test('latest refresh attempt wins without replacing the owner', async () => {
  // Given
  const { store } = fixture()
  const owner = store.capture()
  const first = deferredUser()
  const second = deferredUser()
  const queue = [first, second]
  const controller = createAuthSessionController(store, () => {
    const next = queue.shift()
    assert.ok(next)
    return next.promise
  })
  const old = controller.refreshUser(owner)
  const latest = controller.refreshUser(owner)
  second.resolve({ ...user, display_name: 'Updated' })
  await latest
  const current = store.getSnapshot()
  // When
  first.resolve(user)
  // Then
  assert.equal(await old, false)
  assert.equal(store.getSnapshot(), current)
  assert.equal(current.user?.display_name, 'Updated')
  assert.equal(store.capture(), owner)
})

for (const failure of [new TypeError('offline'), Object.assign(new Error('unavailable'), { status: 503 })]) {
  test(`validated session survives ${failure.message}`, async () => {
    // Given
    const { store, storage, writes } = fixture()
    const current = store.getSnapshot()
    const count = writes()
    const controller = createAuthSessionController(store, () => Promise.reject(failure))
    // When
    const refreshed = await controller.refreshUser(store.capture())
    // Then
    assert.equal(refreshed, false)
    assert.equal(store.getSnapshot(), current)
    assert.equal(storage.read(), 'token-a')
    assert.equal(writes(), count)
  })
}

test('storage logout adopts latest value without writing and fences pending refresh', async () => {
  // Given
  const { store, values, key, writes } = fixture()
  const pending = deferredUser()
  const controller = createAuthSessionController(store, () => pending.promise)
  const refresh = controller.refreshUser(store.capture())
  values.delete(key)
  const count = writes()
  // When
  store.adoptStoredSession(store.capture())
  pending.resolve(user)
  // Then
  assert.equal(await refresh, false)
  assert.equal(store.getSnapshot().status, 'anonymous')
  assert.equal(values.has(key), false)
  assert.equal(writes(), count)
})

test('storage adoption reads latest token, not a delayed event payload', () => {
  // Given
  const { store, values, key, writes } = fixture()
  values.set(key, 'latest')
  const count = writes()
  // When
  store.adoptStoredSession(store.capture())
  // Then
  assert.equal(store.getSnapshot().token, 'latest')
  assert.equal(store.getSnapshot().status, 'loading')
  assert.equal(writes(), count)
})

test('storage change fences completion even before its event is handled', async () => {
  // Given
  const { store, values, key, writes } = fixture()
  const pending = deferredUser()
  const controller = createAuthSessionController(store, () => pending.promise)
  const refresh = controller.refreshUser(store.capture())
  const current = store.getSnapshot()
  values.set(key, 'other-tab')
  const count = writes()
  // When
  pending.resolve(user)
  // Then
  assert.equal(await refresh, false)
  assert.equal(store.getSnapshot(), current)
  assert.equal(values.get(key), 'other-tab')
  assert.equal(writes(), count)
})

test('platform operators are rejected by both application and refresh', async () => {
  // Given
  const { store, storage } = fixture()
  const operator = { ...user, is_platform_admin: true }
  const controller = createAuthSessionController(store, () => Promise.resolve(operator))
  // When
  const refreshed = await controller.refreshUser(store.capture())
  const applied = store.applySession(store.capture(), { access_token: 'operator', user: operator })
  // Then
  assert.equal(refreshed, false)
  assert.equal(applied, false)
  assert.equal(store.getSnapshot().status, 'anonymous')
  assert.equal(storage.read(), null)
})

test('subscribers observe immutable snapshots and unsubscribe cleanly', () => {
  // Given
  const { store } = fixture()
  const seen: string[] = []
  const unsubscribe = store.subscribe(() => seen.push(store.getSnapshot().status))
  // When
  store.logout(store.capture())
  unsubscribe()
  store.applySession(store.capture(), { access_token: 'new', user, must_change_password: true })
  // Then
  assert.deepEqual(seen, ['anonymous'])
  assert.equal(Object.isFrozen(store.getSnapshot()), true)
  assert.equal(Object.isFrozen(store.getSnapshot().user), true)
  assert.equal(store.getSnapshot().user?.must_change_password, true)
})

test('current refresh 401 expires the session', async () => {
  // Given
  const { store, storage } = fixture()
  const failure = Object.assign(new Error('expired'), { status: 401 })
  const controller = createAuthSessionController(store, () => Promise.reject(failure))
  // When
  const refreshed = await controller.refreshUser(store.capture())
  // Then
  assert.equal(refreshed, false)
  assert.equal(store.getSnapshot().status, 'anonymous')
  assert.equal(storage.read(), null)
})

test('superseded refresh 401 cannot expire the current attempt', async () => {
  // Given
  const { store, storage, writes } = fixture()
  const pending = deferredUser()
  const controller = createAuthSessionController(store, () => pending.promise)
  const old = controller.refreshUser(store.capture())
  const latest = store.beginRefresh(store.capture())
  const current = store.getSnapshot()
  const count = writes()
  // When
  pending.reject(Object.assign(new Error('old expiry'), { status: 401 }))
  // Then
  assert.equal(await old, false)
  assert.equal(store.getSnapshot(), current)
  assert.equal(storage.read(), 'token-a')
  assert.equal(writes(), count)
  assert.ok(latest)
  assert.equal(store.commitRefresh(latest, user), true)
})
