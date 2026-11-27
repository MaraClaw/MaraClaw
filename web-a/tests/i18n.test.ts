import assert from 'node:assert/strict'
import { test } from 'node:test'

import { loadVietnamese, parseLocale, translate, translateOwned } from '../src/lib/i18n.ts'
import { vietnamese } from '../src/lib/translations.ts'

// Module state is shared by the whole file: this test must stay first, before anything loads the dictionary.
test('Vietnamese lookups fall back to the English key until the dictionary is loaded', () => {
  assert.equal(translate('vi', 'Added {name}', { name: 'Ada' }), 'Added Ada')
  assert.equal(translateOwned('vi', 'Added {name}'), 'Added {name}')
})

test('loadVietnamese shares one load and then serves synchronous translations', async () => {
  const first = loadVietnamese()
  assert.equal(loadVietnamese(), first)
  await first
  assert.equal(translate('vi', 'Added {name}', { name: 'Ada' }), vietnamese['Added {name}'].replace('{name}', 'Ada'))
  assert.equal(translate('en', 'Added {name}', { name: 'Ada' }), 'Added Ada')
  assert.equal(translateOwned('vi', 'Added {name}'), vietnamese['Added {name}'])
  assert.equal(translateOwned('vi', 'Not a dictionary key'), 'Not a dictionary key')
  assert.equal(translateOwned('vi', undefined), undefined)
})

test('stored locale accepts en/vi and falls back to English for missing or invalid values', () => {
  for (const [stored, expected] of [
    [null, 'en'], ['', 'en'], ['garbage', 'en'], ['vi-VN', 'en'],
    ['VI', 'en'], ['en', 'en'], ['vi', 'vi'],
  ] as const) {
    assert.equal(parseLocale(stored), expected, `stored locale: ${stored}`)
  }
})

test('translation interpolates all named slots in both locales', async () => {
  await loadVietnamese()
  for (const locale of ['en', 'vi'] as const) {
    const rendered = translate(locale, 'Language: {current}. Switch to {next}', {
      current: 'Alpha', next: 'Beta',
    })
    assert.ok(rendered.includes('Alpha'))
    assert.ok(rendered.includes('Beta'))
    assert.doesNotMatch(rendered, /\{[^{}]+\}/)
  }
})

test('translation interpolates zero and empty string without treating them as missing', () => {
  assert.equal(translate('en', 'Page {page}', { page: 0 }), 'Page 0')
  assert.equal(translate('en', 'Page {page}', { page: '' }), 'Page ')
})

test('Vietnamese catalog values are nonempty and preserve the exact interpolation slot set', () => {
  const slots = (value: string) => [...new Set(Array.from(value.matchAll(/\{([^{}]+)\}/g), (match) => match[1]))].sort()
  for (const [key, value] of Object.entries(vietnamese)) {
    assert.ok(value.trim().length > 0, `empty translation: ${key}`)
    assert.deepEqual(slots(value), slots(key), `interpolation slots: ${key}`)
  }
})
