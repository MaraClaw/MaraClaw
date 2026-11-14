import assert from 'node:assert/strict'
import test from 'node:test'

import { CapacityGuardError, capacityCancelErrorKey, capacityEvidenceErrorKey, capacityGuardKey } from '../src/lib/capacity-errors.ts'
import { ApiError } from '../src/lib/http.ts'

const conflict = (code: string) => new ApiError(409, { code, message: 'English text from the engine' })
const nginxPage = '<html><head><title>413 Request Entity Too Large</title></head><body></body></html>'

test('an evidence 413 is recognised by status, from the engine JSON or from nginx HTML alike', () => {
  const fromNginx = new ApiError(413, nginxPage)
  assert.equal(fromNginx.message, 'Request failed (413)')
  for (const error of [fromNginx, new ApiError(413, 'Payment evidence must be 10 MiB or smaller')]) {
    assert.equal(capacityEvidenceErrorKey(error), 'This file is too large to upload. Choose a file up to 10 MiB.')
  }
})

test('every other status the evidence route answers has client copy, and unknown ones keep the engine message', () => {
  assert.equal(capacityEvidenceErrorKey(new ApiError(415, 'Payment evidence must be a PNG, JPEG or WebP image or a PDF')), 'Choose a PNG, JPEG, WebP or PDF file.')
  assert.equal(capacityEvidenceErrorKey(conflict('order_not_awaiting_payment')), 'This order is no longer waiting for payment. Its latest status is being refreshed.')
  assert.equal(capacityEvidenceErrorKey(new ApiError(503, 'Could not store the payment evidence; try again')), 'Could not store the evidence. Try again in a moment.')
  assert.equal(capacityEvidenceErrorKey(new ApiError(404, 'Capacity order not found')), 'This order does not exist, or it belongs to another company.')
  assert.equal(capacityEvidenceErrorKey(new ApiError(403, 'Only an organization admin may buy or manage extra accounts and agents')), 'Only an organization admin of this company can do this.')
  for (const error of [conflict('order_not_cancellable'), new ApiError(409, 'no code'), new ApiError(422, 'file is required'), new ApiError(500, 'boom'), new TypeError('offline'), null]) {
    assert.equal(capacityEvidenceErrorKey(error), null)
  }
})

test('cancelling maps its own conflict code and the order access errors, nothing from the upload', () => {
  assert.equal(capacityCancelErrorKey(conflict('order_not_cancellable')), 'Only an order that is waiting for payment can be cancelled. Its latest status is being refreshed.')
  assert.equal(capacityCancelErrorKey(new ApiError(404, 'Capacity order not found')), 'This order does not exist, or it belongs to another company.')
  assert.equal(capacityCancelErrorKey(new ApiError(403, 'Organization admins may only read their own company')), 'Only an organization admin of this company can do this.')
  for (const error of [conflict('order_not_awaiting_payment'), new ApiError(413, nginxPage), new ApiError(503, 'down'), new TypeError('offline')]) {
    assert.equal(capacityCancelErrorKey(error), null)
  }
})

test('a local refusal is a 403 ApiError that carries its own translation key', () => {
  const refusal = new CapacityGuardError('Cancelling an order is not available for this account.')
  assert.ok(refusal instanceof ApiError)
  assert.equal(refusal.status, 403)
  assert.equal(refusal.message, 'Cancelling an order is not available for this account.')
  assert.equal(capacityGuardKey(refusal), refusal.key)
  assert.equal(capacityEvidenceErrorKey(refusal), refusal.key)
  assert.equal(capacityCancelErrorKey(refusal), refusal.key)
  assert.equal(capacityGuardKey(new ApiError(403, 'Only an organization admin may buy or manage extra accounts and agents')), null)
})
