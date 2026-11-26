import assert from 'node:assert/strict'
import { test } from 'node:test'
import { vietnamese } from '../src/lib/i18n/catalog.ts'
import {
  createTranslator, isTranslationKey, LANGUAGE_STORAGE_KEY, loadVietnamese, parseLocale, persistLocale, readLocale, translateOwned,
} from '../src/lib/i18n/translation.ts'

// Module state is shared by the whole file: this test must stay first, before anything loads the catalog.
test('Given the Vietnamese strings are not loaded, every lookup falls back to its English key', () => {
  assert.equal(isTranslationKey('Retry connection'), false)
  assert.equal(translateOwned('vi', 'Retry connection'), 'Retry connection')
  assert.equal(createTranslator('vi')('{count} attempts', { count: 3 }), '3 attempts')
})

test('Given loadVietnamese, one shared load serves every synchronous caller with today\'s answers', async () => {
  const first = loadVietnamese()
  assert.equal(loadVietnamese(), first)
  await first
  assert.equal(isTranslationKey('Retry connection'), true)
  assert.equal(isTranslationKey('Not a catalog key'), false)
  assert.equal(translateOwned('vi', 'Retry connection'), vietnamese['Retry connection'])
  assert.equal(translateOwned('en', 'Retry connection'), 'Retry connection')
  assert.equal(translateOwned('vi', 'Not a catalog key'), 'Not a catalog key')
  await loadVietnamese()
})

test('Given the loaded catalog, every key is recognised and translates to its own value', async () => {
  await loadVietnamese()
  for (const [key, value] of Object.entries(vietnamese)) {
    assert.ok(isTranslationKey(key), key)
    assert.equal(translateOwned('vi', key), value)
    assert.equal(translateOwned('en', key), key)
  }
})

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

test('Given named slots, translators interpolate values without translating user content', async () => {
  await loadVietnamese()
  assert.equal(createTranslator('en')('Signed in as {account}', { account: 'Save' }), 'Signed in as Save')
  assert.equal(createTranslator('vi')('Signed in as {account}', { account: 'Save' }), 'Đã đăng nhập với tên Save')
  assert.equal(createTranslator('en')('{count} attempts', { count: 3 }), '3 attempts')
  assert.equal(createTranslator('vi')('{count} attempts', { count: 3 }), '3 lần thử')
})

test('Given the chat connection notices, Vietnamese renders its own copy for each', async () => {
  await loadVietnamese()
  const translate = createTranslator('vi')
  for (const notice of [
    'You’re offline. Chat will reconnect when your connection is back.',
    'Connection to the agent was lost.',
    'Retry connection',
  ] as const) {
    assert.notEqual(translate(notice), notice)
  }
})

test('Every Vietnamese message is nonempty and preserves exactly the English interpolation slots', () => {
  const slots = (text: string) => [...new Set(Array.from(text.matchAll(/\{(\w+)\}/g), (match) => match[1]))].sort()
  for (const [key, value] of Object.entries(vietnamese)) {
    assert.ok(value.trim().length > 0, `Empty translation: ${key}`)
    assert.deepEqual(slots(value), slots(key), `Interpolation slots differ: ${key}`)
  }
})
