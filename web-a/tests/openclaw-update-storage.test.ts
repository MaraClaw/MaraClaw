import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { createOpenClawUpdateStorage } from '../src/lib/openclaw-update-storage.ts'

const request = { title: 'Release', body: 'Details', mandatory: true, request_id: '8243c4c8-520e-40c5-8b1d-dff2b258957f' }

function storageFixture() {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
  return { values, storage }
}

test('restores the exact immutable request for only its admin after a remount', () => {
  // Given
  const { storage } = storageFixture()
  createOpenClawUpdateStorage('first-admin', () => storage).save(request)
  // When
  const restored = createOpenClawUpdateStorage('first-admin', () => storage).read()
  // Then
  assert.deepEqual(restored, request)
  assert.equal(Object.isFrozen(restored), true)
  assert.equal(createOpenClawUpdateStorage('other-admin', () => storage).read(), null)
})

test('clearing one admin leaves another admin retry intact', () => {
  // Given
  const { storage } = storageFixture()
  const first = createOpenClawUpdateStorage('first-admin', () => storage)
  const other = createOpenClawUpdateStorage('other-admin', () => storage)
  first.save(request); other.save({ ...request, mandatory: false })
  // When
  first.clear()
  // Then
  assert.equal(first.read(), null)
  assert.deepEqual(other.read(), { ...request, mandatory: false })
})

test('rejects corrupt saved requests without silently discarding them', () => {
  // Given
  const { storage, values } = storageFixture()
  const saved = createOpenClawUpdateStorage('first-admin', () => storage)
  saved.save(request)
  for (const key of values.keys()) values.set(key, JSON.stringify({ ...request, request_id: 'invalid' }))
  // When / Then
  assert.throws(() => saved.read(), z.ZodError)
  assert.equal(values.size, 1)
})

test('propagates unavailable storage instead of claiming the request was saved', () => {
  // Given
  const denied = new Error('Storage unavailable')
  const saved = createOpenClawUpdateStorage('first-admin', () => { throw denied })
  // When / Then
  assert.throws(() => saved.save(request), (error: unknown) => error === denied)
})

for (const operation of ['read', 'clear'] satisfies readonly ('read' | 'clear')[]) {
  test(`propagates unavailable storage during ${operation} without claiming recovery`, () => {
    // Given
    const denied = new Error('Storage unavailable')
    const saved = createOpenClawUpdateStorage('first-admin', () => { throw denied })
    // When / Then
    assert.throws(() => saved[operation](), (error: unknown) => error === denied)
  })
}
