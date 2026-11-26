import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isTranslationKey, loadVietnamese } from '../src/lib/i18n/translation.ts'
import { ApiError } from '../src/lib/http.ts'
import {
  isOrganizationQuoteLapsed, organizationAdmissionErrorKey, RECEIPT_MAX_BYTES, receiptFileProblem,
} from '../src/lib/organization-admission-errors.ts'

await loadVietnamese()

const conflict = (code: string) => new ApiError(409, { code, message: 'Engine message.' })

test('each refused 409 code and each file or rate refusal maps to its own translated message', () => {
  const keys = [
    conflict('email_registered'), conflict('admission_already_pending'), conflict('domain_claimed'),
    conflict('payments_not_configured'), conflict('quote_already_used'),
    new ApiError(413, 'The file is too large.'), new ApiError(415, 'Unsupported file.'), new ApiError(429, { code: 'too_many_requests', message: 'Slow down.' }),
  ].map(organizationAdmissionErrorKey)
  for (const key of keys) assert.ok(key !== null && isTranslationKey(key), String(key))
  assert.equal(new Set(keys).size, keys.length)
})

test('unknown codes and failures without a mapped refusal fall through to the engine message', () => {
  for (const error of [conflict('something_new'), new ApiError(409, 'plain'), new ApiError(503, 'Unavailable'), new ApiError(422, { detail: [] }), new Error('offline')]) {
    assert.equal(organizationAdmissionErrorKey(error), null)
  }
})

test('only a 400 or 410 means the quote can no longer be submitted', () => {
  assert.equal(isOrganizationQuoteLapsed(new ApiError(400, { code: 'quote_invalid', message: 'Invalid.' })), true)
  assert.equal(isOrganizationQuoteLapsed(new ApiError(410, { code: 'quote_expired', message: 'Expired.' })), true)
  for (const status of [409, 413, 429, 503]) assert.equal(isOrganizationQuoteLapsed(new ApiError(status, 'x')), false)
  assert.equal(isOrganizationQuoteLapsed(new Error('offline')), false)
})

test('receipt files are checked for type, emptiness and the 10 MiB cap, advisory only', () => {
  assert.equal(receiptFileProblem({ name: 'receipt.png', type: 'image/png', size: 1 }), null)
  assert.equal(receiptFileProblem({ name: 'receipt.pdf', type: 'application/pdf', size: RECEIPT_MAX_BYTES }), null)
  assert.equal(receiptFileProblem({ name: 'receipt.JPG', type: '', size: 1 }), null)
  assert.equal(receiptFileProblem({ name: 'receipt.webp', type: 'application/octet-stream', size: 1 }), null)
  for (const file of [
    { name: 'notes.txt', type: 'text/plain', size: 1 },
    { name: 'receipt', type: '', size: 1 },
    { name: 'receipt.png', type: 'image/png', size: 0 },
    { name: 'receipt.png', type: 'image/png', size: RECEIPT_MAX_BYTES + 1 },
  ]) {
    const problem = receiptFileProblem(file)
    assert.ok(problem !== null && isTranslationKey(problem), JSON.stringify(file))
  }
})
