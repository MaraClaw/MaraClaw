import assert from 'node:assert/strict'
import test from 'node:test'

import { parseLocale, translate } from '../src/lib/i18n.ts'
import { vietnamese } from '../src/lib/translations.ts'

test('stored locale accepts only supported codes and defaults to English', () => {
  for (const value of [null, '', 'garbage', 'vi-VN', 'fr', 'EN']) {
    assert.equal(parseLocale(value), 'en')
  }
  assert.equal(parseLocale('en'), 'en')
  assert.equal(parseLocale('vi'), 'vi')
})

test('translation interpolates named slots without interpreting replacement values', () => {
  const values = { name: '$& {other}' }
  assert.equal(translate('en', 'Added {name}', values), 'Added $& {other}')
  assert.equal(translate('vi', 'Added {name}', values), 'Đã thêm $& {other}')
})

test('every Vietnamese catalog entry preserves the source interpolation contract', () => {
  const slots = (value: string) => [...new Set([...value.matchAll(/\{([^{}]+)\}/g)].map((match) => match[1]))].sort()
  for (const [source, translation] of Object.entries(vietnamese)) {
    assert.notEqual(translation.trim(), '', `Empty translation: ${source}`)
    assert.deepEqual(slots(translation), slots(source), `Interpolation mismatch: ${source}`)
  }
})
