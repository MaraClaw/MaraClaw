import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'

import { readPreference, writePreference } from '../src/lib/preference-storage.ts'
import { MemoryStorage } from './helpers.ts'

const KEY = 'maraclaw-admin-theme'
const blocked = () => new DOMException('Access is denied for this document.', 'SecurityError')

function installStorage(t: TestContext, localStorage: () => Storage) {
  Object.defineProperty(globalThis, 'window', { value: { get localStorage() { return localStorage() } }, configurable: true })
  t.after(() => { Reflect.deleteProperty(globalThis, 'window') })
}

class RefusingStorage extends MemoryStorage {
  override getItem(): string | null { throw blocked() }
  override setItem(): void { throw new DOMException('The quota has been exceeded.', 'QuotaExceededError') }
}

const refusals = {
  'a blocked localStorage getter': () => { throw blocked() },
  'refused reads and writes': () => new RefusingStorage(),
} satisfies Record<string, () => Storage>

for (const [refusal, localStorage] of Object.entries(refusals)) {
  test(`with ${refusal}, the preference stays in memory without throwing`, (t) => {
    // Given
    installStorage(t, localStorage)
    // When / Then
    assert.equal(readPreference(KEY), null)
    assert.doesNotThrow(() => { writePreference(KEY, 'dark') })
  })
}

test('working storage still saves and restores the preference', (t) => {
  // Given
  const storage = new MemoryStorage()
  installStorage(t, () => storage)
  // When
  writePreference(KEY, 'dark')
  // Then
  assert.equal(storage.getItem(KEY), 'dark')
  assert.equal(readPreference(KEY), 'dark')
})

test('errors other than a storage refusal propagate', (t) => {
  // Given
  const failure = new TypeError('Storage bug')
  installStorage(t, () => { throw failure })
  // When / Then
  assert.throws(() => readPreference(KEY), (error: unknown) => error === failure)
  assert.throws(() => { writePreference(KEY, 'dark') }, (error: unknown) => error === failure)
})
