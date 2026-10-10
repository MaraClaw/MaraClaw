import assert from 'node:assert/strict'
import test from 'node:test'

import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { StaleSessionError, runOwned } from '../src/lib/session-policy.ts'
import { MemoryStorage, createFixture } from './helpers.ts'

test('memory storage replaces a token without changing other keys', () => {
  // Given
  const storage = new MemoryStorage()
  storage.setItem(TOKEN_KEY, 'first')
  storage.setItem('theme', 'dark')
  // When
  storage.setItem(TOKEN_KEY, 'replacement')
  // Then
  assert.equal(storage.getItem(TOKEN_KEY), 'replacement')
  assert.equal(storage.length, 2)
  assert.equal(storage.key(0), TOKEN_KEY)
  assert.equal(storage.getItem('theme'), 'dark')
})

test('memory storage removes only the requested token', () => {
  // Given
  const storage = new MemoryStorage()
  storage.setItem(TOKEN_KEY, 'token-a')
  storage.setItem('theme', 'dark')
  // When
  storage.removeItem(TOKEN_KEY)
  // Then
  assert.equal(storage.getItem(TOKEN_KEY), null)
  assert.equal(storage.getItem('theme'), 'dark')
})

test('memory storage clears every key', () => {
  // Given
  const storage = new MemoryStorage()
  storage.setItem(TOKEN_KEY, 'token-a')
  storage.setItem('theme', 'dark')
  // When
  storage.clear()
  // Then
  assert.equal(storage.length, 0)
  assert.equal(storage.key(0), null)
})

test('a new login retires the generation even when the JWT is identical', () => {
  // Given
  const { policy } = createFixture()
  const previous = policy.capture()
  // When
  const current = policy.replace('token-a')
  // Then
  assert.equal(current.generation, previous.generation + 1)
  assert.equal(previous.signal.aborted, true)
  assert.equal(policy.isCurrent(previous), false)
  assert.equal(policy.isCurrent(current), true)
})

test('logout while anonymous retires a pending login owner', () => {
  // Given
  const { policy } = createFixture(null)
  const previous = policy.capture()
  // When
  const current = policy.logout()
  // Then
  assert.equal(current.generation, previous.generation + 1)
  assert.equal(policy.isCurrent(previous), false)
  assert.equal(previous.signal.aborted, true)
})

test('the stored token fence rejects work before a queued storage event arrives', () => {
  // Given
  const { storage, policy } = createFixture()
  const previous = policy.capture()
  // When
  storage.setItem(TOKEN_KEY, 'token-b')
  // Then
  assert.equal(policy.isCurrent(previous), false)
  assert.throws(() => policy.assertCurrent(previous), StaleSessionError)
  assert.equal(policy.expire(previous), false)
  assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
})

for (const outcome of ['success', 'failure'] as const) {
  test(`late owned ${outcome} is rejected after token ABA replacement`, async () => {
    // Given
    const { policy } = createFixture()
    const previous = policy.capture()
    const deferred = Promise.withResolvers<string>()
    const result = runOwned(policy, previous, () => deferred.promise)
    const rejected = assert.rejects(result, StaleSessionError)
    // When
    policy.replace('token-b')
    policy.replace('token-a')
    if (outcome === 'success') deferred.resolve('retired data')
    else deferred.reject(new Error('retired failure'))
    // Then
    await rejected
    assert.equal(policy.capture().token, 'token-a')
    assert.equal(policy.isCurrent(previous), false)
  })
}

test('an owned failure is preserved while its session is current', async () => {
  // Given
  const { policy } = createFixture()
  const failure = new TypeError('network failure')
  // When
  const result = runOwned(policy, policy.capture(), () => Promise.reject(failure))
  // Then
  await assert.rejects(result, (error) => error === failure)
  assert.equal(policy.capture().token, 'token-a')
})
