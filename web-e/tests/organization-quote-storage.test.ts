import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'

import {
  clearOrganizationQuote, ORGANIZATION_QUOTE_KEY, readOrganizationQuote, storeOrganizationQuote,
} from '../src/lib/organization-quote-storage.ts'
import { OrganizationAdmissionQuoteSchema } from '../src/lib/engine-contracts.generated.ts'
import { quote as wireQuote } from './organization-quote-fixture.ts'

class MemoryStorage implements Storage {
  readonly values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, value) }
}

const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
beforeEach(() => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: new MemoryStorage() })
})
afterEach(() => {
  if (previousStorage) Object.defineProperty(globalThis, 'sessionStorage', previousStorage)
  else Reflect.deleteProperty(globalThis, 'sessionStorage')
})

const quote = OrganizationAdmissionQuoteSchema.parse(wireQuote)
const beforeExpiry = Date.parse(quote.expires_at) - 1000
const atExpiry = Date.parse(quote.expires_at)

test('a stored quote reads back unchanged until its price expires', () => {
  storeOrganizationQuote(quote)
  assert.deepEqual(readOrganizationQuote(beforeExpiry), quote)
  assert.deepEqual(readOrganizationQuote(beforeExpiry), quote)
})

test('an expired quote is dropped from storage', () => {
  storeOrganizationQuote(quote)
  assert.equal(readOrganizationQuote(atExpiry), null)
  assert.equal(sessionStorage.getItem(ORGANIZATION_QUOTE_KEY), null)
})

test('a malformed stored value is dropped instead of trusted', () => {
  for (const stored of ['not json', '{}', JSON.stringify({ ...quote, amount_vnd: '174750000' }), JSON.stringify({ ...quote, extra: true }), JSON.stringify({ ...quote, expires_at: 'soon' })]) {
    sessionStorage.setItem(ORGANIZATION_QUOTE_KEY, stored)
    assert.equal(readOrganizationQuote(beforeExpiry), null, stored)
    assert.equal(sessionStorage.getItem(ORGANIZATION_QUOTE_KEY), null, stored)
  }
})

test('nothing stored reads as no quote and clearing removes a stored one', () => {
  assert.equal(readOrganizationQuote(beforeExpiry), null)
  storeOrganizationQuote(quote)
  clearOrganizationQuote()
  assert.equal(readOrganizationQuote(beforeExpiry), null)
})
