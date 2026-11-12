import assert from 'node:assert/strict'
import { test } from 'node:test'
import { vietnamese } from '../src/lib/i18n/catalog.ts'
import { createTranslator, LANGUAGE_STORAGE_KEY, parseLocale, persistLocale, readLocale } from '../src/lib/i18n/translation.ts'

test('Given absent or invalid locale values, parsing and storage reads default to English', () => {
  for (const value of [null, undefined, '', 'garbage', 'en-US', 'VI', 42]) {
    assert.equal(parseLocale(value), 'en')
  }
  assert.equal(parseLocale('en'), 'en')
  assert.equal(parseLocale('vi'), 'vi')
  for (const value of [null, 'garbage', 'en', 'vi']) {
    assert.equal(readLocale({ getItem(key) { assert.equal(key, LANGUAGE_STORAGE_KEY); return value } }), value === 'vi' ? 'vi' : 'en')
  }
})

test('Given restricted storage, reads fall back and persistence does not prevent a language switch', () => {
  for (const error of [new DOMException('Blocked', 'SecurityError'), 'restricted']) {
    assert.equal(readLocale({ getItem() { throw error } }), 'en')
    assert.doesNotThrow(() => persistLocale('vi', { setItem() { throw error } }))
  }
  const writes: [string, string][] = []
  persistLocale('vi', { setItem(key, value) { writes.push([key, value]) } })
  assert.deepEqual(writes, [[LANGUAGE_STORAGE_KEY, 'vi']])
})

test('Given named slots, translators interpolate values without translating user content', () => {
  assert.equal(createTranslator('en')('Signed in as {account}', { account: 'Save' }), 'Signed in as Save')
  assert.equal(createTranslator('vi')('Signed in as {account}', { account: 'Save' }), 'Đã đăng nhập với tên Save')
  assert.equal(createTranslator('en')('{count} attempts', { count: 3 }), '3 attempts')
  assert.equal(createTranslator('vi')('{count} attempts', { count: 3 }), '3 lần thử')
})

test('Every Vietnamese message is nonempty and preserves exactly the English interpolation slots', () => {
  const slots = (text: string) => [...new Set(Array.from(text.matchAll(/\{(\w+)\}/g), (match) => match[1]))].sort()
  for (const [key, value] of Object.entries(vietnamese)) {
    assert.ok(value.trim().length > 0, `Empty translation: ${key}`)
    assert.deepEqual(slots(value), slots(key), `Interpolation slots differ: ${key}`)
  }
})
