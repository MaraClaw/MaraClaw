import assert from 'node:assert/strict'
import test from 'node:test'

import { loginSchema } from '../src/lib/login-schema.ts'

test('email login normalizes the identifier while preserving the password and wire key', () => {
  // Given
  const input = { login_identifier: '  Operator@Example.test  ', password: ' exact password ' }
  // When
  const credentials = loginSchema.parse(input)
  // Then
  assert.deepEqual(credentials, {
    login_identifier: 'operator@example.test', password: ' exact password ',
  })
})

for (const identifier of ['operator', '+15551234567', '', '  ', 'operator@', '@example.test']) {
  test(`email login rejects a non-email identifier: ${JSON.stringify(identifier)}`, () => {
    // Given
    const input = { login_identifier: identifier, password: 'test-password' }
    // When
    const result = loginSchema.safeParse(input)
    // Then
    assert.equal(result.success, false)
    if (!result.success) assert.deepEqual(result.error.issues.map((issue) => issue.path), [['login_identifier']])
  })
}

test('email login still requires a password', () => {
  // Given
  const input = { login_identifier: 'operator@example.test', password: '' }
  // When
  const result = loginSchema.safeParse(input)
  // Then
  assert.equal(result.success, false)
  if (!result.success) assert.deepEqual(result.error.issues.map((issue) => issue.path), [['password']])
})
