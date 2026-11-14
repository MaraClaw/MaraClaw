import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { z } from 'zod'

import { adminSession } from '../src/lib/admin-session.ts'
import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import {
  cancelCapacityOrder, capacityConflict, createCapacityOrder, decideCapacityOrder, fetchCapacityEvidence, fetchCapacityOrder,
  fetchCapacityOrders, fetchCapacityOverview, fetchCapacityPaymentSettings, fetchCapacityReviewOrders, saveCapacityPaymentSettings,
  submitCapacityEvidence,
} from '../src/lib/capacity-api.ts'
import { ApiError, BinaryLimitError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { MemoryStorage } from './helpers.ts'
import { capacityOrderId, capacityTenantId, cappedOverview, purchaseOrder, submittedOrder } from './capacity-fixtures.ts'

type Call = readonly [url: RequestInfo | URL, init?: RequestInit]
const respond = (body: unknown, status = 200) => async (..._call: Call) => Response.json(body, { status })

function signIn(t: TestContext, token = 'token-a') {
  const storage = new MemoryStorage()
  storage.setItem(TOKEN_KEY, token)
  Object.defineProperty(globalThis, 'window', { value: { localStorage: storage }, configurable: true })
  adminSession.reconcile()
  t.after(() => {
    Reflect.deleteProperty(globalThis, 'window')
    adminSession.reconcile()
  })
  return storage
}

const receipt = () => new File([new Uint8Array([137, 80, 78, 71])], 'receipt.png', { type: 'image/png' })

test('the overview reads the named company with a cancellation signal', async (t) => {
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', respond(cappedOverview))
  const result = await fetchCapacityOverview(capacityTenantId, controller.signal)
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/capacity/overview?tenant_id=${capacityTenantId}`)
  assert.equal(init?.method, 'GET')
  assert.equal(init?.signal, controller.signal)
  assert.deepEqual(result, cappedOverview)
  assert.equal(Object.isFrozen(result), true)
})

test('an organization admin reads its own company without a tenant filter', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond(cappedOverview))
  await fetchCapacityOverview(null)
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/capacity/overview?')
})

test('a company scope that is not a UUID never reaches the network', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond(cappedOverview))
  await assert.rejects(fetchCapacityOverview('tenant-a'), z.ZodError)
  await assert.rejects(fetchCapacityOrders({ tenantId: 'tenant-a' }), z.ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

for (const malformed of [
  { ...cappedOverview, today: 'yesterday' }, { ...cappedOverview, capped: 'yes' }, { ...cappedOverview, remaining_days: 17.5 },
  { ...cappedOverview, open_orders: [{ ...purchaseOrder, status: 'paid' }] }, { ...cappedOverview, current_renewal: { ...purchaseOrder, id: 'not-a-uuid' } },
  { ...cappedOverview, tier: 'enterprise' },
]) {
  test(`the overview rejects a malformed response (${JSON.stringify(malformed).slice(0, 60)})`, async (t) => {
    t.mock.method(globalThis, 'fetch', respond(malformed))
    await assert.rejects(fetchCapacityOverview(null), z.ZodError)
  })
}

test('the order list sends bounded paging and the optional company', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond({ items: [purchaseOrder], has_more: true }))
  const page = await fetchCapacityOrders({ tenantId: capacityTenantId, limit: 10, offset: 20 })
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/capacity/orders?limit=10&offset=20&tenant_id=${capacityTenantId}`)
  assert.deepEqual(page.items, [purchaseOrder])
  assert.equal(page.has_more, true)
  await fetchCapacityOrders({ tenantId: null })
  assert.equal(fetch.mock.calls[1]?.arguments[0], '/api/capacity/orders?limit=50&offset=0')
  for (const bad of [{ limit: 0 }, { limit: 101 }, { offset: -1 }, { limit: 1.5 }]) {
    await assert.rejects(fetchCapacityOrders({ tenantId: null, ...bad }), z.ZodError)
  }
  assert.equal(fetch.mock.callCount(), 2)
})

test('an order carries the locked quote, recipient and nullable review fields', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond({ ...purchaseOrder, unexpected: 'dropped' }))
  const order = await fetchCapacityOrder(capacityOrderId)
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/capacity/orders/${capacityOrderId}`)
  assert.deepEqual(order, purchaseOrder)
  assert.equal(order.recipient.account_number, '0123456789')
})

test('creating an order sends strict whole numbers as JSON', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond(purchaseOrder, 201))
  const order = await createCapacityOrder({ extra_accounts: 2, extra_agents: 3 })
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/capacity/orders')
  assert.equal(init?.method, 'POST')
  assert.equal(init?.body, JSON.stringify({ extra_accounts: 2, extra_agents: 3 }))
  assert.equal(new Headers(init?.headers).get('Content-Type'), 'application/json')
  assert.equal(order.id, capacityOrderId)
})

for (const invalid of [
  { extra_accounts: 0, extra_agents: 0 }, { extra_accounts: 10_001, extra_agents: 0 }, { extra_accounts: 0, extra_agents: 10_001 },
  { extra_accounts: 1.5, extra_agents: 1 }, { extra_accounts: -1, extra_agents: 2 },
]) {
  test(`an invalid order fails before transport (${JSON.stringify(invalid)})`, async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch', respond(purchaseOrder, 201))
    await assert.rejects(createCapacityOrder(invalid), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('the three purchase refusals are told apart by their code', async (t) => {
  for (const [code, order_id] of [['payments_not_configured', undefined], ['purchase_already_open', capacityOrderId], ['not_eligible', undefined]] as const) {
    t.mock.method(globalThis, 'fetch', respond({ detail: { code, message: 'Refused', ...(order_id ? { order_id } : {}) } }, 409))
    const error = await createCapacityOrder({ extra_accounts: 1, extra_agents: 0 }).catch((reason: unknown) => reason)
    assert.ok(error instanceof ApiError)
    assert.equal(error.message, 'Refused')
    assert.deepEqual(capacityConflict(error), { code, orderId: order_id ?? null })
  }
})

test('only a 409 with a code is a conflict', () => {
  assert.equal(capacityConflict(new ApiError(409, 'Already decided')), null)
  assert.equal(capacityConflict(new ApiError(409, { code: 7 })), null)
  assert.equal(capacityConflict(new ApiError(403, { code: 'not_eligible' })), null)
  assert.equal(capacityConflict(new ApiError(409, { code: 'purchase_already_open', order_id: 'not-a-uuid' })), null)
  assert.equal(capacityConflict(new Error('offline')), null)
  assert.equal(capacityConflict(null), null)
})

test('cancelling posts to the order without a body', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond({ ...purchaseOrder, status: 'cancelled' }))
  const order = await cancelCapacityOrder(capacityOrderId)
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/capacity/orders/${capacityOrderId}/cancel`)
  assert.equal(init?.method, 'POST')
  assert.equal(init?.body, undefined)
  assert.equal(order.status, 'cancelled')
})

test('finishing uploads the file as multipart without a JSON content type and parses the submitted order', async (t) => {
  signIn(t)
  const fetch = t.mock.method(globalThis, 'fetch', respond(submittedOrder))
  const order = await submitCapacityEvidence(capacityOrderId, receipt(), 'receipt.png')
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/capacity/orders/${capacityOrderId}/evidence`)
  assert.equal(init?.method, 'POST')
  const headers = new Headers(init?.headers)
  assert.equal(headers.has('Content-Type'), false)
  assert.equal(headers.get('Authorization'), 'Bearer token-a')
  assert.equal(headers.get('Accept'), 'application/json')
  const body = init?.body
  assert.ok(body instanceof FormData)
  assert.deepEqual([...body.keys()], ['file'])
  const sent = body.get('file')
  assert.ok(sent instanceof File)
  assert.deepEqual([sent.name, sent.type, sent.size], ['receipt.png', 'image/png', 4])
  assert.equal(order.status, 'submitted')
  assert.equal(order.evidence?.filename, 'receipt.png')
})

test('an upload response that is not an order is rejected', async (t) => {
  t.mock.method(globalThis, 'fetch', respond({ ...submittedOrder, status: 'paid' }))
  await assert.rejects(submitCapacityEvidence(capacityOrderId, receipt(), 'receipt.png'), z.ZodError)
})

for (const [status, detail] of [
  [413, 'Payment evidence must be 10 MiB or smaller'], [415, 'Payment evidence must be a PNG, JPEG, WebP or PDF file'],
  [409, { code: 'order_not_awaiting_payment', message: 'This order no longer accepts payment evidence' }], [404, 'Order not found'],
] as const) {
  test(`an upload refused with ${status} keeps its status and message`, async (t) => {
    t.mock.method(globalThis, 'fetch', respond({ detail }, status))
    await assert.rejects(submitCapacityEvidence(capacityOrderId, receipt(), 'receipt.png'), (error) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, status)
      assert.equal(error.message, typeof detail === 'string' ? detail : detail.message)
      return true
    })
  })
}

test('an upload that finishes after sign-out has no effect', async (t) => {
  signIn(t)
  const started = Promise.withResolvers<void>()
  const response = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const pending = submitCapacityEvidence(capacityOrderId, receipt(), 'receipt.png')
  const rejected = assert.rejects(pending, StaleSessionError)
  await started.promise
  adminSession.logout()
  response.resolve(Response.json(submittedOrder))
  await rejected
})

test('the evidence download is authenticated, uncached and redirect-free', async (t) => {
  signIn(t)
  const fetch = t.mock.method(globalThis, 'fetch', async (..._call: Call) => new Response(new Uint8Array([1, 2, 3]), { headers: { 'Content-Type': 'image/png' } }))
  const controller = new AbortController()
  const bytes = await fetchCapacityEvidence(capacityOrderId, controller.signal)
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/capacity/orders/${capacityOrderId}/evidence`)
  assert.equal(init?.method, 'GET')
  assert.equal(init?.cache, 'no-store')
  assert.equal(init?.redirect, 'error')
  assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer token-a')
  assert.deepEqual([...bytes], [1, 2, 3])
})

test('a download past 20 MiB stops with the preview limit error', async (t) => {
  const chunk = new Uint8Array(8 * 1024 * 1024)
  let sent = 0
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (sent === 3) controller.close()
      else { sent += 1; controller.enqueue(chunk) }
    },
  })
  t.mock.method(globalThis, 'fetch', async (..._call: Call) => new Response(body))
  await assert.rejects(fetchCapacityEvidence(capacityOrderId), BinaryLimitError)
  assert.ok(sent <= 3)
})

test('a missing evidence file is a typed 404', async (t) => {
  t.mock.method(globalThis, 'fetch', respond({ detail: 'Payment evidence not found' }, 404))
  await assert.rejects(fetchCapacityEvidence(capacityOrderId), (error) => error instanceof ApiError && error.status === 404 && error.message === 'Payment evidence not found')
})

test('the review queue sends the status and offset', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond({ items: [submittedOrder], has_more: false }))
  const page = await fetchCapacityReviewOrders('submitted', 50)
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/capacity-orders?status=submitted&limit=50&offset=50')
  assert.equal(page.items[0]?.tenant_name, 'Acme Co')
  await fetchCapacityReviewOrders('awaiting_payment')
  assert.equal(fetch.mock.calls[1]?.arguments[0], '/api/admin/capacity-orders?status=awaiting_payment&limit=50&offset=0')
  await assert.rejects(fetchCapacityReviewOrders('paid' as 'submitted'), z.ZodError)
  await assert.rejects(fetchCapacityReviewOrders('submitted', -1), z.ZodError)
  assert.equal(fetch.mock.callCount(), 2)
})

test('an approval omits an absent note and a rejection carries its note', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond({ ...submittedOrder, status: 'approved' }))
  await decideCapacityOrder(capacityOrderId, { action: 'approve' })
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/admin/capacity-orders/${capacityOrderId}/decision`)
  assert.equal(init?.method, 'POST')
  assert.equal(init?.body, '{"action":"approve"}')
  await decideCapacityOrder(capacityOrderId, { action: 'reject', note: 'The amount does not match.' })
  assert.equal(fetch.mock.calls[1]?.arguments[1]?.body, JSON.stringify({ action: 'reject', note: 'The amount does not match.' }))
})

for (const [label, decision] of [
  ['a rejection without a note', { action: 'reject' }], ['a rejection with a blank note', { action: 'reject', note: '   ' }],
  ['a note over 1000 characters', { action: 'approve', note: 'x'.repeat(1001) }], ['an unknown action', { action: 'refund' }],
] as const) {
  test(`${label} fails before transport`, async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch', respond(submittedOrder))
    await assert.rejects(decideCapacityOrder(capacityOrderId, decision as unknown as Parameters<typeof decideCapacityOrder>[1]), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('a note of exactly 1000 characters, counted as code points, is accepted', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', respond({ ...submittedOrder, status: 'rejected' }))
  await decideCapacityOrder(capacityOrderId, { action: 'reject', note: '\u{1F4B8}'.repeat(1000) })
  assert.equal(fetch.mock.callCount(), 1)
})

test('a decision conflict keeps its typed code', async (t) => {
  t.mock.method(globalThis, 'fetch', respond({ detail: { code: 'order_already_decided', message: 'Already decided' } }, 409))
  const error = await decideCapacityOrder(capacityOrderId, { action: 'approve' }).catch((reason: unknown) => reason)
  assert.equal(capacityConflict(error)?.code, 'order_already_decided')
})

test('unconfigured payment settings are all null and configured ones round-trip through a strict PUT', async (t) => {
  const unset = { configured: false, bank_name: null, account_number: null, branch_name: null, usd_vnd_rate: null }
  const saved = { configured: true, bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'District 1 Branch', usd_vnd_rate: 25_000 }
  const fetch = t.mock.method(globalThis, 'fetch', respond(unset))
  assert.deepEqual(await fetchCapacityPaymentSettings(), unset)
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/capacity-payment-settings')
  fetch.mock.mockImplementation(respond(saved))
  const request = { bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'District 1 Branch', usd_vnd_rate: 25_000 }
  assert.deepEqual(await saveCapacityPaymentSettings(request), saved)
  const [url, init] = fetch.mock.calls[1]?.arguments ?? []
  assert.equal(url, '/api/admin/capacity-payment-settings')
  assert.equal(init?.method, 'PUT')
  assert.deepEqual(JSON.parse(String(init?.body)), request)
})

for (const bad of [
  { usd_vnd_rate: 0 }, { usd_vnd_rate: 1_000_001 }, { usd_vnd_rate: 25_000.5 }, { usd_vnd_rate: '25000' }, { bank_name: '' },
  { account_number: 'x'.repeat(121) }, { branch_name: undefined },
]) {
  test(`settings that break the contract fail before transport (${JSON.stringify(bad)})`, async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch', respond({}))
    const request = { bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'District 1 Branch', usd_vnd_rate: 25_000, ...bad }
    await assert.rejects(saveCapacityPaymentSettings(request as Parameters<typeof saveCapacityPaymentSettings>[0]), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('settings refused to a non-genesis admin keep their 403', async (t) => {
  t.mock.method(globalThis, 'fetch', respond({ detail: 'Only the genesis platform admin can read the payment settings' }, 403))
  await assert.rejects(fetchCapacityPaymentSettings(), (error) => error instanceof ApiError && error.status === 403)
})
