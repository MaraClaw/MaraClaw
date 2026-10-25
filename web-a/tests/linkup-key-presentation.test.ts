import assert from 'node:assert/strict'
import test from 'node:test'

import { linkupKeyPresentation } from '../src/lib/linkup-key-presentation.ts'

const now = Date.parse('2026-10-25T12:00:00Z')

for (const expiry of [null, '2026-10-25T11:59:59Z', '2026-10-25T12:00:00Z']) {
  test(`active key is available with elapsed or missing cooldown ${expiry}`, () => {
    // Given
    const key = { status: 'active', exhausted_until: expiry }
    // When
    const result = linkupKeyPresentation(key, now)
    // Then
    assert.equal(result.variant, 'success')
    assert.equal(result.cooldownUntil, null)
  })
}

test('active persisted key presents a future cooldown without altering the DTO', () => {
  // Given
  const key = Object.freeze({ status: 'active', exhausted_until: '2026-10-25T12:00:01Z' })
  // When
  const result = linkupKeyPresentation(key, now)
  // Then
  assert.equal(result.variant, 'warning')
  assert.equal(result.cooldownUntil, key.exhausted_until)
  assert.equal(key.status, 'active')
})

test('disabled key does not become active or cooling down because of an expiry', () => {
  // Given
  const key = { status: 'disabled', exhausted_until: '2026-10-25T12:00:01Z' }
  // When
  const result = linkupKeyPresentation(key, now)
  // Then
  assert.equal(result.variant, 'secondary')
  assert.equal(result.cooldownUntil, null)
})
