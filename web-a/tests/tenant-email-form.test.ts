import assert from 'node:assert/strict'
import test from 'node:test'

import { tenantEmailDraftSchema, tenantEmailToDraft } from '../src/pages/tenant-email-form.ts'

const draft = { enabled: true, from_address: 'sender@example.test', from_name: 'MaraClaw', smtp_host: 'smtp.example.test', smtp_port: '465', smtp_username: 'mailer', password: '', smtp_ssl: true, smtp_timeout_seconds: '15' }

test('blank password draft produces a complete replacement without a password key', () => {
  // Given / When
  const result = tenantEmailDraftSchema.parse(draft)
  // Then
  assert.deepEqual(result, { enabled: true, from_address: 'sender@example.test', from_name: 'MaraClaw', smtp_host: 'smtp.example.test', smtp_port: 465, smtp_username: 'mailer', smtp_ssl: true, smtp_timeout_seconds: 15 })
  assert.equal('password' in result, false)
})

test('replacement password preserves intentional whitespace', () => {
  // Given / When
  const result = tenantEmailDraftSchema.parse({ ...draft, password: ' a secret ' })
  // Then
  assert.equal('password' in result ? result.password : undefined, ' a secret ')
})

test('enabled SMTP accepts an intentionally blank sender name without substituting a brand', () => {
  // Given / When
  const result = tenantEmailDraftSchema.parse({ ...draft, from_name: '' })
  // Then
  assert.equal(result.from_name, '')
})

for (const [name, values] of [
  ['smtp_port', ['', '0', '65536', '1.5', '1e2']],
  ['smtp_timeout_seconds', ['', '0', '31', '1.5', '-1']],
  ['from_address', ['', 'invalid', 'https://example.test']],
  ['smtp_host', ['', 'smtp server', 'https://smtp.example.test', 'smtp.example.test/path', '!!!']],
] as const) for (const value of values) {
  test(`${name}=${JSON.stringify(value)} blocks an enabled configuration`, () => {
    // Given / When
    const result = tenantEmailDraftSchema.safeParse({ ...draft, [name]: value })
    // Then
    assert.equal(result.success, false)
  })
}

for (const smtp_port of ['1', '65535']) for (const smtp_timeout_seconds of ['1', '30']) {
  test(`port ${smtp_port} timeout ${smtp_timeout_seconds} accept exact bounds`, () => {
    // Given / When
    const result = tenantEmailDraftSchema.parse({ ...draft, smtp_port, smtp_timeout_seconds })
    // Then
    assert.equal(result.smtp_port, Number(smtp_port))
    assert.equal(result.smtp_timeout_seconds, Number(smtp_timeout_seconds))
  })
}

test('disabled empty configuration permits organization delivery to remain off without sender/host', () => {
  // Given / When
  const result = tenantEmailDraftSchema.parse({ ...draft, enabled: false, from_address: '', smtp_host: '', smtp_username: '' })
  // Then
  assert.equal(result.enabled, false)
  assert.equal(result.smtp_port, 465)
  assert.equal(result.smtp_ssl, true)
  assert.equal(result.smtp_timeout_seconds, 15)
})

test('saved values populate the form but saved password is always a blank draft', () => {
  // Given
  const saved = { enabled: false, from_address: '', from_name: '', smtp_host: '', smtp_port: 465, smtp_username: '', smtp_ssl: true, smtp_timeout_seconds: 15, has_password: true, effective_source: 'none' } as const
  // When
  const result = tenantEmailToDraft(saved)
  // Then
  assert.equal(result.password, '')
  assert.equal(result.from_name, '')
  assert.equal(result.smtp_port, '465')
  assert.equal('has_password' in result, false)
  assert.equal('effective_source' in result, false)
})
