import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { fetchAdminAudit } from '../src/lib/admin-audit-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { actorId, auditEvent, ownAuditSession, tenantId } from './admin-audit-fixtures.ts'

for (const [role, path] of [
  ['platform_admin', '/api/admin/audit-logs'], ['org_admin', '/api/users/admin-audit-logs'],
] as const) {
  test(`${role} requests its own endpoint with the bounded default and nullable records`, async (context) => {
    // Given
    ownAuditSession(context)
    const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([auditEvent]))
    // When
    const result = await fetchAdminAudit({ role })
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, `${path}?limit=100`)
    assert.equal(options?.method, 'GET')
    assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: 'Bearer synthetic-audit-token' })
    assert.deepEqual(result, [auditEvent])
    assert.equal(Object.isFrozen(result), true)
    assert.equal(Object.isFrozen(result[0]), true)
  })
}

test('platform filters are trimmed and encoded without adding query parameters', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  await fetchAdminAudit({ role: 'platform_admin', tenant_id: ` ${tenantId} `, actor_id: actorId, action: ' user.activate&limit=501 ', limit: 500 })
  // Then
  const url = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
  assert.deepEqual([...url.searchParams], [['limit', '500'], ['action', 'user.activate&limit=501'], ['tenant_id', tenantId], ['actor_id', actorId]])
})

test('org filters send only action and limit', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  await fetchAdminAudit({ role: 'org_admin', action: ' user.activate ', limit: 1 })
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/users/admin-audit-logs?limit=1&action=user.activate')
})

test('org requests reject smuggled tenant or actor overrides before transport', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json([]))
  const overridden = { role: 'org_admin' as const, tenant_id: tenantId, actor_id: actorId }
  // When / Then
  await assert.rejects(fetchAdminAudit(overridden), z.ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

for (const limit of [0, -1, 501, 1.5, NaN, Infinity]) {
  test(`limit ${limit} is rejected before transport`, async (context) => {
    // Given
    const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json([]))
    // When / Then
    await assert.rejects(fetchAdminAudit({ role: 'org_admin', limit }), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('blank filters are omitted rather than submitted as identifiers', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  await fetchAdminAudit({ role: 'platform_admin', action: '  ', tenant_id: '', actor_id: ' ' })
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/audit-logs?limit=100')
})

test('complete records preserve JSON details and timezone timestamps', async (context) => {
  // Given
  const event = { ...auditEvent, actor_id: actorId, actor_email: 'operator@example.test', tenant_id: tenantId, target_id: tenantId, ip_address: '127.0.0.1', created_at: '2026-10-21T12:30:00+08:00' }
  context.mock.method(globalThis, 'fetch', async () => Response.json([event]))
  // When / Then
  assert.deepEqual(await fetchAdminAudit({ role: 'platform_admin' }), [event])
})

for (const malformed of [null, {}, [null], [{ ...auditEvent, id: '../bad' }], [{ ...auditEvent, actor_id: 'actor' }], [{ ...auditEvent, action: null }], [{ ...auditEvent, changes: [] }], [{ ...auditEvent, details: null }], [{ ...auditEvent, created_at: 'yesterday' }], [{ ...auditEvent, actor_email: 42 }], Array.from({ length: 501 }, () => auditEvent)]) {
  test(`malformed response class ${Array.isArray(malformed) ? `${malformed.length} rows/${JSON.stringify(malformed[0])}` : JSON.stringify(malformed)} is rejected`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(fetchAdminAudit({ role: 'platform_admin' }), z.ZodError)
  })
}

test('caller abort propagates even when transport ignores cancellation', async (context) => {
  // Given
  const controller = new AbortController()
  const response = Promise.withResolvers<Response>()
  const fetch = context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, _options?: RequestInit) => response.promise)
  const pending = fetchAdminAudit({ role: 'org_admin' }, controller.signal)
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json([auditEvent]))
  await rejected
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal?.aborted, true)
})

test('force-password 403 remains an error without expiring the current session', async (context) => {
  // Given
  const session = ownAuditSession(context)
  const owner = session.capture()
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { must_change_password: true } }, { status: 403 }))
  // When / Then
  await assert.rejects(fetchAdminAudit({ role: 'org_admin' }), (error) => error instanceof ApiError && error.status === 403)
  assert.equal(session.isCurrent(owner), true)
})
