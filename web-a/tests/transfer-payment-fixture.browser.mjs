import assert from 'node:assert/strict'

import { bounded, responseBarrier, uuid } from './admission-fixture.browser.mjs'
import { pendingTransfer } from './transfer-review-fixtures.ts'

export { bounded, responseBarrier }
export const listPath = '/api/company-admissions/transfers/review'
export const detailPath = `/api/company-admissions/transfers/${pendingTransfer.id}`

export function paymentFixture(receipt, options) {
  const user = {
    id: uuid(9801), identity_id: uuid(9802), email: 'payment-reviewer@example.test', display_name: 'Synthetic Reviewer',
    avatar_url: null, role: options.role ?? 'platform_admin', is_platform_admin: options.platformFlag ?? false,
    is_genesis: options.genesis ?? true, tenant_id: pendingTransfer.source_tenant_id, is_active: true,
    email_verified: true, must_change_password: options.forcePassword ?? false, created_at: '2026-10-20T00:00:00Z',
  }
  const state = {
    ...options, receipt, user, row: structuredClone(pendingTransfer), calls: [], barriers: [], inFlight: new Set(),
    listStatus: 200, detailStatus: 200, decisionStatus: 200, tenantStatus: 200,
  }
  state.middleware = (request, response) => {
    const work = respond(state, request, response)
    state.inFlight.add(work); void work.finally(() => state.inFlight.delete(work))
  }
  return state
}

function reply(state, call) {
  const { path, method, params, payload } = call
  if (path === '/api/auth/me') return { body: state.user }
  if (path === '/api/notifications/unread-count') return { body: { unread_count: 0 } }
  if (path === '/api/tenants/me') return state.tenantStatus === 200
    ? { body: { id: state.user.tenant_id, is_active: true, is_default_end_user_org: state.defaultOrg ?? true } }
    : { status: state.tenantStatus, body: { detail: 'Synthetic tenant access unavailable' } }
  if (path === listPath) {
    assert.equal(method, 'GET'); assert.deepEqual(params, { limit: '50' })
    return state.listStatus === 200 ? { body: state.empty ? [] : [state.row, { ...state.row, id: uuid(9899), choice: 'free' }] }
      : { status: state.listStatus, body: { detail: 'Synthetic payment list unavailable' } }
  }
  if (path === detailPath) return state.detailStatus === 200 ? { body: state.row } : { status: state.detailStatus, body: { detail: 'Synthetic payment detail unavailable' } }
  assert.equal(path, `${detailPath}/decision`, `Unexpected API ${path}`)
  assert.equal(method, 'POST'); assert.deepEqual(Object.keys(payload).sort(), ['decision', 'reason'])
  assert.ok(['approved', 'rejected'].includes(payload.decision))
  assert.ok(payload.reason === null || (typeof payload.reason === 'string' && payload.reason.length <= 2000))
  if ([403, 503].includes(state.decisionStatus)) return { status: state.decisionStatus, body: { detail: 'Synthetic payment decision unavailable' } }
  const approved = state.decisionStatus === 409 || payload.decision === 'approved'
  Object.assign(state.row, {
    status: approved ? 'admission_created' : 'rejected', review_reason: payload.reason,
    reviewed_at: '2026-10-24T10:00:00Z', reviewed_by_user_id: state.user.id,
    payment_confirmed_at: approved ? '2026-10-24T10:00:00Z' : null,
    admission_request_id: approved ? uuid(9888) : null,
  })
  return state.decisionStatus === 409 ? { status: 409, body: { detail: 'Synthetic concurrent review' } } : { body: state.row }
}

async function respond(state, incoming, outgoing) {
  let barrier
  try {
    const chunks = []
    for await (const chunk of incoming) chunks.push(chunk)
    const raw = Buffer.concat(chunks).toString('utf8'), url = new URL(incoming.url, 'http://synthetic.invalid')
    const call = { scenario: state.name, path: url.pathname, method: incoming.method, params: Object.fromEntries(url.searchParams), payload: raw ? JSON.parse(raw) : undefined }
    const response = structuredClone(reply(state, call))
    call.status = response.status ?? 200
    state.calls.push(call); state.receipt.requests.push(call)
    barrier = state.barriers.find((item) => !item.released && item.match(call))
    if (barrier) { barrier.enter(call); await barrier.wait }
    if (!outgoing.destroyed) { outgoing.writeHead(call.status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); outgoing.end(JSON.stringify(response.body)) }
  } catch (error) {
    state.receipt.violations.push({ scenario: state.name, message: String(error), path: incoming.url })
    if (!outgoing.destroyed) { outgoing.writeHead(502); outgoing.end('{"detail":"Synthetic payment firewall blocked this request"}') }
  } finally { barrier?.complete() }
}
