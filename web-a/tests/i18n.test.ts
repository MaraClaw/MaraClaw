import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseLocale, translate } from '../src/lib/i18n.ts'
import { vietnamese } from '../src/lib/translations.ts'

test('stored locale accepts en/vi and falls back to English for missing or invalid values', () => {
  for (const [stored, expected] of [
    [null, 'en'], ['', 'en'], ['garbage', 'en'], ['vi-VN', 'en'],
    ['VI', 'en'], ['en', 'en'], ['vi', 'vi'],
  ] as const) {
    assert.equal(parseLocale(stored), expected, `stored locale: ${stored}`)
  }
})

test('translation interpolates all named slots in both locales', () => {
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
