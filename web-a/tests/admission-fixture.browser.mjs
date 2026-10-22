// Synthetic HTTP contracts only. No backend, persisted accounts, or offset fallback.
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

export const listPath = '/api/company-admissions/review/page'
export const uuid = (number) => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`
const tenant = { id: uuid(8001), name: 'Synthetic Admission Review Organization' }
const admin = {
  id: uuid(8002), identity_id: uuid(8002), email: 'admission-reviewer@example.test',
  display_name: 'Synthetic Genesis Reviewer', avatar_url: null, role: 'org_admin',
  is_platform_admin: false, is_genesis: true, tenant_id: tenant.id, is_active: true,
  email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
export function makeRow(number) {
  return {
    id: uuid(number), status: 'pending', source_tenant_id: uuid(8003), target_tenant: tenant,
    method: ['invitation', 'verified_domain', 'sso'][number % 3],
    created_at: `2026-10-21T${number > 55 ? '09' : '08'}:00:00Z`, decided_at: null,
    decision_reason: null, applicant: { id: uuid(10000 + number), email: `synthetic-applicant-${number}@example.test` },
    source_tenant: { id: uuid(8003), name: 'Synthetic Source Organization — International Operations' },
    decided_by_user_id: null,
  }
}
const before = (left, right) => left.created_at < right.created_at || (left.created_at === right.created_at && left.id < right.id)
export const ordered = (rows, status = 'pending') => rows.filter((row) => row.status === status).sort((a, b) => before(a, b) ? 1 : before(b, a) ? -1 : 0)
export async function bounded(promise, message = 'Synthetic browser operation timed out') {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), 15_000) })]) }
  finally { clearTimeout(timer) }
}
export function responseBarrier(state, match) {
  const entered = Promise.withResolvers(), released = Promise.withResolvers(), completed = Promise.withResolvers(), cancelled = Promise.withResolvers()
  const barrier = {
    match, entered: entered.promise, wait: released.promise, completed: completed.promise, cancelled: cancelled.promise,
    requests: [], released: false,
    enter(call) { this.requests.push(call); entered.resolve(call) },
    complete() { completed.resolve() }, cancel(call) { cancelled.resolve(call) },
    release() { if (!this.released) { this.released = true; state.receipt.cleanup.barriersReleased++; released.resolve() } },
  }
  state.barriers.push(barrier); state.receipt.cleanup.barriersCreated++
  return barrier
}
export function makeFixture(receipt, name) {
  const state = {
    receipt, name, rows: Array.from({ length: 101 }, (_, index) => makeRow(index + 1)),
    calls: [], barriers: [], tokens: new Map(), events: new EventEmitter(), inFlight: new Set(),
    listStatus: 200, detailStatus: 200, decisionStatus: 200,
  }
  state.waitCall = async (since, match) => {
    const found = state.calls.slice(since).find(match)
    if (found) return found
    let listener
    try {
      return await bounded(new Promise((resolve) => {
        listener = (call) => { if (match(call)) resolve(call) }
        state.events.on('call', listener)
      }), `Expected synthetic request after call ${since}`)
    } finally { state.events.off('call', listener) }
  }
  state.middleware = (incoming, outgoing) => {
    const work = respond(state, incoming, outgoing)
    state.inFlight.add(work); void work.finally(() => state.inFlight.delete(work))
  }
  return state
}
function admissionResponse(state, call) {
  const { path, method, params, payload } = call
  if (path === listPath) {
    assert.equal(method, 'GET'); assert.equal(params.limit, '50')
    assert.ok(['pending', 'approved', 'rejected', 'withdrawn'].includes(params.status))
    assert.ok(Object.keys(params).every((key) => ['status', 'limit', 'cursor'].includes(key)), 'Cursor wire must never contain offset')
    if (state.listStatus !== 200) return { status: state.listStatus, body: { detail: 'Synthetic admission list unavailable' } }
    const anchor = params.cursor ? state.tokens.get(params.cursor) : null
    assert.ok(!params.cursor || anchor?.status === params.status, 'Cursor must be previously issued for this filter')
    const remaining = ordered(state.rows, params.status).filter((row) => !anchor || before(row, anchor))
    const items = remaining.slice(0, 50), last = items.at(-1)
    const cursor = remaining.length > 50 ? `synthetic/+?=review:${params.status}:${last.created_at}:${last.id}` : null
    if (cursor) state.tokens.set(cursor, { ...last, status: params.status })
    return { body: { items, next_cursor: cursor } }
  }
  const match = /^\/api\/company-admissions\/([\da-f-]+)(\/decision)?$/u.exec(path)
  assert.ok(match, `Unexpected admission route ${path}; legacy offset endpoint is forbidden`)
  const row = state.rows.find((item) => item.id === match[1])
  assert.ok(row, 'Only synthetic IDs may be accessed')
  if (!match[2]) {
    assert.equal(method, 'GET')
    return state.detailStatus === 200 ? { body: row } : { status: state.detailStatus, body: { detail: 'Synthetic admission detail unavailable' } }
  }
  assert.equal(method, 'POST'); assert.deepEqual(Object.keys(payload).sort(), ['decision', 'reason'])
  assert.ok(['approved', 'rejected'].includes(payload.decision)); assert.equal(row.status, 'pending')
  assert.ok(payload.reason === null || (typeof payload.reason === 'string' && payload.reason === payload.reason.trim() && payload.reason.length <= 2000))
  if (state.decisionStatus === 503) return { status: 503, body: { detail: 'Synthetic admission decision unavailable; retry safely.' } }
  Object.assign(row, {
    status: state.decisionStatus === 409 ? 'approved' : payload.decision,
    decision_reason: state.decisionStatus === 409 ? 'Synthetic concurrent reviewer decision' : payload.reason,
    decided_at: '2026-10-22T03:00:00Z', decided_by_user_id: admin.id,
  })
  return state.decisionStatus === 409 ? { status: 409, body: { detail: 'Synthetic request changed concurrently' } } : { body: row }
}
async function respond(state, incoming, outgoing) {
  let barrier, call
  const url = new URL(incoming.url, 'http://synthetic.invalid')
  try {
    const chunks = []
    for await (const chunk of incoming) chunks.push(chunk)
    const text = Buffer.concat(chunks).toString('utf8')
    call = { scenario: state.name, method: incoming.method, path: url.pathname.replace(/\/$/u, ''), params: Object.fromEntries(url.searchParams), payload: text ? JSON.parse(text) : undefined }
    let response
    if (call.path.startsWith('/api/company-admissions/')) response = admissionResponse(state, call)
    else {
      assert.equal(call.method, 'GET', 'Ancillary endpoints are read-only')
      assert.ok(['/api/auth/me', '/api/notifications/unread-count'].includes(call.path), `Blocked unrecognized API ${call.path}`)
      response = { body: call.path === '/api/auth/me' ? admin : { unread_count: 0 } }
    }
    // Freeze at request time: a held old response must stay old after queue changes.
    response = structuredClone(response)
    Object.assign(call, { status: response.status ?? 200, response: response.body, cancelled: false })
    state.receipt.requests.push(call)
    if (call.path.startsWith('/api/company-admissions/')) { state.calls.push(call); state.events.emit('call', call) }
    barrier = state.barriers.find((item) => !item.released && item.match(call))
    outgoing.once('close', () => {
      if (!outgoing.writableEnded) { call.cancelled = true; state.receipt.transport.cancelledResponses++; barrier?.cancel(call) }
    })
    if (barrier) { barrier.enter(call); await barrier.wait }
    if (!outgoing.destroyed) {
      outgoing.writeHead(call.status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
      outgoing.end(JSON.stringify(response.body))
    }
  } catch (error) {
    state.receipt.violations.push({ scenario: state.name, path: url.pathname, message: String(error) })
    if (!outgoing.destroyed) { outgoing.writeHead(502, { 'Content-Type': 'application/json' }); outgoing.end('{"detail":"Blocked by synthetic admission firewall"}') }
  } finally { barrier?.complete() }
}
