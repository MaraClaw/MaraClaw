// Synthetic organization admission HTTP records, never forwarded to an engine.
import assert from 'node:assert/strict'
import { bounded, responseBarrier, uuid } from './admission-fixture.browser.mjs'

export { bounded, responseBarrier }
export const listPath = '/api/admin/organization-admissions'
const reviewer = {
  id: uuid(8002), identity_id: uuid(8002), email: 'synthetic-reviewer@example.test',
  display_name: 'Synthetic Platform Reviewer', avatar_url: null, role: 'platform_admin',
  is_platform_admin: true, is_genesis: true, tenant_id: null, is_active: true,
  email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
export function makeFixture(receipt, name) {
  const row = (number, organizationName) => ({
    id: uuid(number), organization_name: organizationName, email: `founder-${number}@example.test`,
    tier: 'max', plan: { id: 'max', name: 'Max', currency: 'USD', monthly_price_usd: 199,
      included_accounts: 5, additional_account_monthly_usd: 12, max_accounts: 75,
      max_agents_per_account: 5, storage_limit_bytes: 1073741824, max_file_size_bytes: 26214400 },
    status: 'pending', created_at: '2026-11-01T08:00:00Z', reviewed_at: null, reviewed_by: null, review_note: null,
    credential_email_status: null, credential_email_error: null,
  })
  const delivered = (number, status) => ({ ...row(number, `Synthetic Delivery ${status}`), status: 'approved',
    reviewed_at: '2026-11-02T08:00:00Z', reviewed_by: reviewer.id, review_note: 'Approved synthetic request',
    credential_email_status: status, credential_email_error: status === 'failed' ? 'smtp_unavailable' : null })
  const state = { receipt, name, rows: [row(1, 'Synthetic International Operations'), row(2, null), delivered(3, 'pending'), delivered(4, 'sent'), delivered(5, 'failed')],
    barriers: [], inFlight: new Set(), calls: [], listStatus: 200, decisionStatus: 200, retryStatus: 200 }
  state.middleware = (incoming, outgoing) => {
    const work = respond(state, incoming, outgoing)
    state.inFlight.add(work); void work.finally(() => state.inFlight.delete(work))
  }
  return state
}
async function respond(state, incoming, outgoing) {
  let barrier
  const url = new URL(incoming.url, 'http://synthetic.invalid')
  try {
    const chunks = []
    for await (const chunk of incoming) chunks.push(chunk)
    const raw = Buffer.concat(chunks).toString('utf8')
    const call = { scenario: state.name, path: url.pathname.replace(/\/$/u, ''), method: incoming.method,
      params: Object.fromEntries(url.searchParams), payload: raw ? JSON.parse(raw) : undefined }
    let body, status = 200
    if (call.path === '/api/auth/me') body = reviewer
    else if (call.path === '/api/notifications/unread-count') body = { unread_count: 0 }
    else if (call.path === listPath) {
      assert.equal(call.method, 'GET'); assert.equal(call.params.limit, '50')
      assert.ok(['pending', 'approved', 'rejected'].includes(call.params.status))
      assert.ok(['0', '50'].includes(call.params.offset))
      status = state.listStatus
      const rows = state.rows.filter((item) => item.status === call.params.status)
      body = status === 200 ? { items: call.params.offset === '0' ? rows : [], has_more: rows.length > 0 && call.params.offset === '0' }
        : { detail: 'Synthetic organization queue unavailable' }
    } else {
      const match = /^\/api\/admin\/organization-admissions\/([\da-f-]+)\/(decision|retry-email)$/u.exec(call.path)
      assert.ok(match, `Unknown synthetic endpoint ${call.path}`); assert.equal(call.method, 'POST')
      const item = state.rows.find((candidate) => candidate.id === match[1]); assert.ok(item)
      if (match[2] === 'retry-email') {
        assert.equal(call.payload, undefined); assert.equal(item.status, 'approved'); assert.equal(item.credential_email_status, 'failed')
        status = state.retryStatus
        if (status === 200) { Object.assign(item, { credential_email_status: 'pending', credential_email_error: null }); body = item }
        else body = { detail: status === 409 ? 'The administrator has changed the password. Original credentials cannot be resent.' : 'Synthetic email retry unavailable.' }
      } else {
        assert.ok(['approve', 'reject'].includes(call.payload.decision))
        assert.ok(Object.keys(call.payload).every((key) => ['decision', 'note'].includes(key)))
        status = state.decisionStatus
        if (status === 200) {
          assert.ok(call.payload.decision !== 'approve' || item.organization_name !== null, 'Legacy approval must never be sent')
          Object.assign(item, { status: call.payload.decision === 'approve' ? 'approved' : 'rejected',
            credential_email_status: call.payload.decision === 'approve' ? 'pending' : null,
            reviewed_at: '2026-11-02T08:00:00Z', reviewed_by: reviewer.id, review_note: call.payload.note ?? '' })
          body = item
        } else body = { detail: status === 409 ? 'An organization with this name already exists.' : 'Synthetic provisioning unavailable.' }
      }
    }
    Object.assign(call, { status, response: structuredClone(body) }); state.calls.push(call); state.receipt.requests.push(call)
    barrier = state.barriers.find((candidate) => !candidate.released && candidate.match(call))
    if (barrier) { barrier.enter(call); await barrier.wait }
    if (!outgoing.destroyed) { outgoing.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); outgoing.end(JSON.stringify(body)) }
  } catch (error) {
    state.receipt.violations.push({ path: url.pathname, message: String(error) })
    if (!outgoing.destroyed) { outgoing.writeHead(502); outgoing.end('{"detail":"Synthetic firewall blocked request"}') }
  } finally { barrier?.complete() }
}
