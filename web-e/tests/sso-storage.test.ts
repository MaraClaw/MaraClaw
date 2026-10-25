import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'

import {
  consumeSsoTransaction,
  readSsoContinuation,
  SSO_CONTINUATION_KEY,
  SSO_INTENT_KEY,
  storeSsoTransaction,
} from '../src/lib/sso-storage.ts'

class MemoryStorage implements Storage {
  readonly values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, value) }
}

const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
beforeEach(() => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: new MemoryStorage() })
})
afterEach(() => {
  if (previousStorage) Object.defineProperty(globalThis, 'sessionStorage', previousStorage)
  else Reflect.deleteProperty(globalThis, 'sessionStorage')
})

const transaction = { state: 'random-state', provider: 'example' }
const path = `/transfer#code=${encodeURIComponent('opaque/code &?#=%')}`

test('SSO restores the exact local invitation only for its transaction', () => {
  // Given
  storeSsoTransaction(transaction, { from: { pathname: path } })
  // When
  const destination = readSsoContinuation(transaction, transaction)
  // Then
  assert.equal(destination, path)
})

for (const pathname of [
  'https://evil.example/transfer#code=invite', '//evil.example/transfer#code=invite',
  '/\\evil.example/transfer#code=invite', 'javascript:alert(1)', '/app', '/login',
  '/transfer?redirect=https://evil.example#code=invite', '/transfer#code=invite&next=/app',
  '/transfer#code=invite#extra', '/transfer#code=', `/transfer#code=${'x'.repeat(257)}`,
  '/transfer#code=%E0%A4%A', '/transfer#code=has+space',
]) {
  test(`SSO discards unsafe or noncanonical router continuation ${pathname}`, () => {
    // Given a prior invitation that must not survive a new sign-in.
    storeSsoTransaction(transaction, { from: { pathname: path } })
    // When
    storeSsoTransaction(transaction, { from: { pathname } })
    // Then
    assert.equal(readSsoContinuation(transaction, transaction), null)
    assert.equal(sessionStorage.getItem(SSO_CONTINUATION_KEY), null)
  })
}

for (const callback of [
  { state: null, provider: 'example' },
  { state: 'another-state', provider: 'example' },
  { state: transaction.state, provider: 'another-provider' },
]) {
  test(`SSO refuses invitation continuation for callback ${JSON.stringify(callback)}`, () => {
    // Given
    storeSsoTransaction(transaction, { from: { pathname: path } })
    // When
    const destination = readSsoContinuation(transaction, callback)
    // Then
    assert.equal(destination, null)
  })
}

for (const stored of [
  'invalid-json', 'null', JSON.stringify({ ...transaction, path: '//evil.example/transfer#code=invite' }),
  JSON.stringify({ ...transaction, state: 'another-state', path }),
  JSON.stringify({ ...transaction, provider: 'another-provider', path }),
]) {
  test(`SSO refuses malformed or unrelated stored continuation ${stored}`, () => {
    // Given
    storeSsoTransaction(transaction, null)
    sessionStorage.setItem(SSO_CONTINUATION_KEY, stored)
    // When
    const destination = readSsoContinuation(transaction, transaction)
    // Then
    assert.equal(destination, null)
  })
}

test('SSO bind intent never resumes an invitation', () => {
  // Given
  storeSsoTransaction(transaction, { from: { pathname: path } })
  // When
  const destination = readSsoContinuation({ ...transaction, intent: 'bind' }, transaction)
  // Then
  assert.equal(destination, null)
})

test('new SSO login replaces stale bind intent and invitation continuation', () => {
  // Given
  storeSsoTransaction(transaction, { from: { pathname: path } })
  sessionStorage.setItem(SSO_INTENT_KEY, 'bind')
  // When
  storeSsoTransaction({ state: 'new-state', provider: 'other-provider' }, null)
  // Then
  assert.equal(sessionStorage.getItem(SSO_INTENT_KEY), null)
  assert.equal(sessionStorage.getItem(SSO_CONTINUATION_KEY), null)
})

test('consuming SSO clears its sensitive transaction without clearing unrelated storage', () => {
  // Given
  storeSsoTransaction(transaction, { from: { pathname: path } })
  sessionStorage.setItem('unrelated', 'preserved')
  // When
  consumeSsoTransaction(transaction)
  // Then
  assert.equal(sessionStorage.length, 1)
  assert.equal(sessionStorage.getItem('unrelated'), 'preserved')
})

for (const replacement of [
  { state: 'new-state', provider: transaction.provider },
  { state: transaction.state, provider: 'other-provider' },
]) {
  test(`late SSO cleanup preserves newer transaction ${JSON.stringify(replacement)}`, () => {
    // Given
    storeSsoTransaction(transaction, null)
    storeSsoTransaction(replacement, { from: { pathname: path } })
    // When
    consumeSsoTransaction(transaction)
    // Then
    assert.equal(readSsoContinuation(replacement, replacement), path)
  })
}
