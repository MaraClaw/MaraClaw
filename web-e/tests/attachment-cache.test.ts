import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ATTACHMENT_CACHE_BYTES } from '../src/lib/attachment-cache.ts'
import { ApiError, BinaryLimitError, MAX_PREVIEW_BYTES, StaleSessionError } from '../src/lib/http.ts'
import { attachmentFixture, member, resource } from './attachment-fixture.ts'

for (const retirement of ['logout', 'replacement', 'tenant-switch'] as const) {
  test(`attachment bytes clear on ${retirement} and stale completions cannot refill them`, (context) => {
    // Given
    const fixture = attachmentFixture()
    context.after(fixture.dispose)
    const { cache, store, scope } = fixture
    const key = cache.authorize(scope.owner, resource, 'v1')
    cache.put(key, new Uint8Array([1, 2, 3]))
    // When
    if (retirement === 'logout') store.logout(scope.owner)
    else store.applySession(scope.owner, { access_token: 'token-a', user: {
      ...member, tenant_id: retirement === 'tenant-switch' ? 'tenant-b' : 'tenant-a',
    } })
    // Then
    assert.equal(cache.size, 0)
    assert.throws(() => cache.put(key, new Uint8Array([4])), StaleSessionError)
  })
}

test('attachment LRU evicts least recently used bytes within the shared 64 MiB budget', (context) => {
  // Given
  const fixture = attachmentFixture()
  context.after(fixture.dispose)
  const { cache, scope } = fixture
  const keys = ['a', 'b', 'c', 'd'].map((path) => cache.authorize(scope.owner, { ...resource, path }, 'v1'))
  const [first, second, third, fourth] = keys
  assert.ok(first && second && third && fourth)
  for (const key of [first, second, third]) cache.put(key, new Uint8Array(MAX_PREVIEW_BYTES))
  cache.read(first)
  // When
  cache.put(fourth, new Uint8Array(MAX_PREVIEW_BYTES))
  // Then
  assert.equal(cache.read(second), undefined)
  assert.ok(cache.read(first))
  assert.ok(cache.size <= ATTACHMENT_CACHE_BYTES)
})

test('an entry larger than 20 MiB cannot enter the cache', (context) => {
  // Given
  const fixture = attachmentFixture()
  context.after(fixture.dispose)
  const key = fixture.cache.authorize(fixture.scope.owner, resource, 'v1')
  // When / Then
  assert.throws(() => fixture.cache.put(key, new Uint8Array(MAX_PREVIEW_BYTES + 1)), BinaryLimitError)
})

test('new authorization evicts old versions and rejects their delayed bytes', (context) => {
  // Given
  const fixture = attachmentFixture()
  context.after(fixture.dispose)
  const { cache, scope } = fixture
  const old = cache.authorize(scope.owner, resource, 'v1')
  cache.put(old, new Uint8Array([1]))
  // When
  cache.authorize(scope.owner, resource, 'v2')
  // Then
  assert.equal(cache.read(old), undefined)
  assert.throws(() => cache.put(old, new Uint8Array([2])), (error: unknown) => error instanceof ApiError && error.status === 409)
})
