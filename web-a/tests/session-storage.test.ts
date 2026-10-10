import assert from 'node:assert/strict'
import test from 'node:test'

import { createAuthSession } from '../src/lib/auth-session.ts'
import { TOKEN_KEY, listenForTokenChanges } from '../src/lib/auth-storage.ts'
import { MemoryStorage, adminUser, createFixture, storageTarget, tokenResponse } from './helpers.ts'

test('other-tab replacement advances once and refreshes the replacement administrator', async (t) => {
  // Given
  const { policy, storage } = createFixture()
  const target = storageTarget(storage)
  const requests: string[] = []
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async (token) => { requests.push(token); return adminUser(token) },
    loginRequest: async () => tokenResponse(),
  })
  t.after(auth.connect())
  await auth.refreshUser()
  const previous = policy.capture()
  const authenticated = Promise.withResolvers<void>()
  t.after(auth.subscribe(() => {
    if (auth.getSnapshot().status === 'authenticated' && auth.getSnapshot().owner.token === 'token-b') {
      authenticated.resolve()
    }
  }))
  t.after(listenForTokenChanges(target, () => { policy.reconcile() }))
  requests.length = 0
  // When
  storage.setItem(TOKEN_KEY, 'token-b')
  target.emit({ key: TOKEN_KEY, storageArea: storage, newValue: 'token-b' })
  target.emit({ key: TOKEN_KEY, storageArea: storage, newValue: 'token-b' })
  await authenticated.promise
  // Then
  assert.equal(auth.getSnapshot().owner.generation, previous.generation + 1)
  assert.equal(previous.signal.aborted, true)
  assert.equal(auth.getSnapshot().user?.id, 'token-b')
  assert.deepEqual(requests, ['token-b'])
})

for (const action of ['removal', 'clear'] as const) {
  test(`other-tab ${action} retires auth exactly once`, async (t) => {
    // Given
    const { policy, storage } = createFixture()
    storage.setItem('maraclaw-admin-theme', 'dark')
    const target = storageTarget(storage)
    const auth = createAuthSession(policy, {
      fetchCurrentUser: async () => adminUser(), loginRequest: async () => tokenResponse(),
    })
    t.after(auth.connect())
    await auth.refreshUser()
    t.after(listenForTokenChanges(target, () => { policy.reconcile() }))
    const previous = policy.capture()
    // When
    if (action === 'clear') storage.clear()
    else storage.removeItem(TOKEN_KEY)
    const key = action === 'clear' ? null : TOKEN_KEY
    target.emit({ key, storageArea: storage, newValue: null })
    target.emit({ key, storageArea: storage, newValue: null })
    // Then
    assert.equal(auth.getSnapshot().status, 'anonymous')
    assert.equal(auth.getSnapshot().user, null)
    assert.equal(auth.getSnapshot().owner.generation, previous.generation + 1)
    assert.equal(previous.signal.aborted, true)
  })
}

test('theme and another storage area do not reconcile the admin token', (t) => {
  // Given
  const { policy, storage } = createFixture()
  const target = storageTarget(storage)
  const previous = policy.capture()
  let reconciliations = 0
  t.after(listenForTokenChanges(target, () => { reconciliations++; policy.reconcile() }))
  // When
  target.emit({ key: 'maraclaw-admin-theme', storageArea: storage, newValue: 'dark' })
  target.emit({ key: TOKEN_KEY, storageArea: new MemoryStorage(), newValue: 'token-b' })
  // Then
  assert.equal(reconciliations, 0)
  assert.strictEqual(policy.capture(), previous)
  assert.equal(previous.signal.aborted, false)
})

test('a queued stale storage notice cannot replace the actual current token', (t) => {
  // Given
  const { policy, storage } = createFixture()
  const target = storageTarget(storage)
  t.after(listenForTokenChanges(target, () => { policy.reconcile() }))
  const current = policy.replace('token-b')
  // When
  target.emit({ key: TOKEN_KEY, storageArea: storage, newValue: 'token-a' })
  // Then
  assert.strictEqual(policy.capture(), current)
  assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
  assert.equal(current.signal.aborted, false)
})

test('StrictMode storage setup and cleanup retain one live listener and remove it', () => {
  // Given
  const { policy, storage } = createFixture()
  const target = storageTarget(storage)
  let reconciliations = 0
  const reconcile = () => { reconciliations++; policy.reconcile() }
  const firstCleanup = listenForTokenChanges(target, reconcile)
  firstCleanup()
  const finalCleanup = listenForTokenChanges(target, reconcile)
  // When
  storage.setItem(TOKEN_KEY, 'token-b')
  target.emit({ key: TOKEN_KEY, storageArea: storage })
  // Then
  assert.equal(reconciliations, 1)
  assert.equal(target.listenerCount, 1)
  finalCleanup()
  target.emit({ key: TOKEN_KEY, storageArea: storage })
  assert.equal(target.listenerCount, 0)
  assert.equal(reconciliations, 1)
})
