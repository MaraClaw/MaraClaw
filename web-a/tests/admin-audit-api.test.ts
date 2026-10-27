import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ADMIN_AUDIT_MAX_CURSOR_LENGTH, adminAuditPageScopeSchema, fetchAdminAudit, fetchAdminAuditPage } from '../src/lib/admin-audit-api.ts'
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

for (const [role, path] of [
  ['platform_admin', '/api/admin/audit-logs/page'], ['org_admin', '/api/users/admin-audit-logs/page'],
] as const) {
  test(`${role} page requests preserve opaque cursors and full timestamp precision`, async (context) => {
    // Given
    ownAuditSession(context)
    const cursor = ' opaque+/=&cursor=other '
    const item = { ...auditEvent, created_at: '2026-10-21T12:30:00.123456+08:00' }
    const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ items: [item], next_cursor: cursor }))
    // When
    const result = await fetchAdminAuditPage({ role, cursor, action: ' user.activate ', limit: 1 })
    // Then
    const [request, options] = fetch.mock.calls[0]?.arguments ?? []
    const url = new URL(String(request), 'https://example.test')
    assert.equal(url.pathname, path)
    assert.deepEqual([...url.searchParams], [['limit', '1'], ['action', 'user.activate'], ['cursor', cursor]])
    assert.equal(options?.method, 'GET')
    assert.deepEqual(result, { items: [item], next_cursor: cursor })
    assert.equal(Object.isFrozen(result), true)
    assert.equal(Object.isFrozen(result.items), true)
  })
}

test('platform root page omits cursor and preserves effective server filters', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
  // When
  const result = await fetchAdminAuditPage({ role: 'platform_admin', tenant_id: ` ${tenantId} `, actor_id: actorId, action: '  ' })
  // Then
  const url = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
  assert.deepEqual([...url.searchParams], [['limit', '100'], ['tenant_id', tenantId], ['actor_id', actorId]])
  assert.deepEqual(result, { items: [], next_cursor: null })
})

for (const scope of [
  { role: 'org_admin' as const, cursor: '' },
  { role: 'org_admin' as const, cursor: 'x'.repeat(ADMIN_AUDIT_MAX_CURSOR_LENGTH + 1) },
  { role: 'org_admin' as const, tenant_id: tenantId, actor_id: actorId },
  ...[0, 501, 1.5].map((limit) => ({ role: 'platform_admin' as const, limit })),
]) {
  test(`invalid page input ${JSON.stringify(scope).slice(0, 100)} fails before transport`, async (context) => {
    // Given
    const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
    // When / Then
    await assert.rejects(fetchAdminAuditPage(scope), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

for (const [index, body] of [
  [], {}, { items: [] }, { items: [], next_cursor: 1 }, { items: [], next_cursor: '' },
  { items: [], next_cursor: 'x'.repeat(ADMIN_AUDIT_MAX_CURSOR_LENGTH + 1) },
  { items: [{ ...auditEvent, created_at: 'bad' }], next_cursor: null },
  { items: Array.from({ length: 501 }, () => auditEvent), next_cursor: null },
].entries()) {
  test(`malformed page envelope ${index} is rejected`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(fetchAdminAuditPage({ role: 'org_admin' }), z.ZodError)
  })
}

test('cancelling an older page rejects late transport data', async (context) => {
  // Given
  const controller = new AbortController()
  const response = Promise.withResolvers<Response>()
  const fetch = context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, _options?: RequestInit) => response.promise)
  const pending = fetchAdminAuditPage({ role: 'org_admin', cursor: 'opaque' }, controller.signal)
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json({ items: [auditEvent], next_cursor: null }))
  await rejected
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal?.aborted, true)
})

test('invalid server cursor remains a recoverable API error in the current session', async (context) => {
  // Given
  const session = ownAuditSession(context)
  const owner = session.capture()
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Invalid cursor' }, { status: 400 }))
  // When / Then
  await assert.rejects(fetchAdminAuditPage({ role: 'org_admin', cursor: 'stale' }), (error) => error instanceof ApiError && error.status === 400)
  assert.equal(session.isCurrent(owner), true)
})

for (const role of ['platform_admin', 'org_admin'] as const) {
  for (const bounds of [
    { start_at: ' 2026-10-21T12:30:00.123456+08:00 ', end_at: '2026-10-21t00:30:00.123457-05:00' },
    { start_at: '2026-10-21T04:30:00.123456Z', end_at: '2026-10-21T05:30:00.123457Z' },
  ]) test(`${role} transmits the same UTC window for ${bounds.start_at}`, async (context) => {
    // Given
    const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
    // When
    await fetchAdminAuditPage({ role, ...bounds, cursor: 'window-page-2' })
    // Then
    const url = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
    assert.equal(url.pathname, role === 'platform_admin' ? '/api/admin/audit-logs/page' : '/api/users/admin-audit-logs/page')
    assert.deepEqual(Object.fromEntries(url.searchParams), { limit: '100', cursor: 'window-page-2', start_at: '2026-10-21T04:30:00.123456Z', end_at: '2026-10-21T05:30:00.123457Z' })
  })

  for (const bounds of [{ start_at: '2026-10-21T00:00:00Z' }, { end_at: '2026-10-22T00:00:00Z' }, { start_at: ' ', end_at: '' }]) {
    test(`${role} supports optional window bounds ${JSON.stringify(bounds)}`, async (context) => {
      // Given
      const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
      // When
      await fetchAdminAuditPage({ role, ...bounds })
      // Then
      const url = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
      assert.equal(url.searchParams.get('start_at'), bounds.start_at?.trim() ? '2026-10-21T00:00:00.000000Z' : null)
      assert.equal(url.searchParams.get('end_at'), bounds.end_at ? '2026-10-22T00:00:00.000000Z' : null)
    })
  }

  for (const value of ['1700000000', 'yesterday', '2026-10-21T00:00:00', '2026-02-30T00:00:00Z', '2026-10-21T00:00Z', '2026-10-21T00:00:00+24:00', '0001-01-01T00:00:00+01:00']) {
    test(`${role} rejects invalid bound ${value} before transport`, async (context) => {
      // Given
      const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
      // When / Then
      for (const field of ['start_at', 'end_at']) await assert.rejects(fetchAdminAuditPage({ role, [field]: value }), z.ZodError)
      assert.equal(fetch.mock.callCount(), 0)
    })
  }

  for (const end_at of ['2026-10-21T08:00:00+08:00', '2026-10-20T23:59:59Z', '2026-10-21T00:00:00.0000009Z']) {
    test(`${role} rejects equal or reversed normalized bounds ending ${end_at}`, async (context) => {
      // Given
      const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
      // When / Then
      await assert.rejects(fetchAdminAuditPage({ role, start_at: '2026-10-21T00:00:00Z', end_at }), z.ZodError)
      assert.equal(fetch.mock.callCount(), 0)
    })
  }
}

test('numeric and null window bounds are rejected without datetime coercion', () => {
  // Given / When / Then
  for (const value of [1700000000, null]) for (const field of ['start_at', 'end_at']) {
    assert.throws(() => adminAuditPageScopeSchema.parse({ role: 'org_admin', [field]: value }), z.ZodError)
  }
})
