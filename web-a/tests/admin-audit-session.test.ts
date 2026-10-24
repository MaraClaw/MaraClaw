import assert from 'node:assert/strict'
import test from 'node:test'
import { QueryObserver, isCancelledError } from '@tanstack/react-query'

import { adminAuditOptions, adminAuditPageOptions } from '../src/hooks/admin-audit-options.ts'
import { createSessionQueryScope, ownQueryClient } from '../src/hooks/session-query-client.ts'
import { adminAuditLogSchema, adminAuditPageSchema, fetchAdminAudit, fetchAdminAuditPage, type AdminAuditPageScope, type AdminAuditScope } from '../src/lib/admin-audit-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { actorId, auditEvent, ownAuditSession, tenantId } from './admin-audit-fixtures.ts'
import { DeferredBodyResponse } from './helpers.ts'

test('every supported role and server filter participates in query identity', () => {
  // Given
  const scopes: readonly AdminAuditScope[] = [
    { role: 'org_admin' }, { role: 'platform_admin' },
    { role: 'platform_admin', tenant_id: tenantId }, { role: 'platform_admin', actor_id: actorId },
    { role: 'platform_admin', action: 'user.activate' }, { role: 'platform_admin', limit: 500 },
  ]
  // When
  const identities = scopes.map((scope) => JSON.stringify(adminAuditOptions(scope).queryKey))
  // Then
  assert.equal(new Set(identities).size, scopes.length)
  assert.deepEqual(adminAuditOptions({ role: 'platform_admin', action: '  user.activate ' }).queryKey,
    adminAuditOptions({ role: 'platform_admin', action: 'user.activate', limit: 100 }).queryKey)
})

test('changing filters aborts the old query and does not display its late records', async (context) => {
  // Given
  const session = ownAuditSession(context)
  const { client } = createSessionQueryScope(session.capture())
  context.after(() => client.clear())
  const response = Promise.withResolvers<Response>()
  let oldSignal: AbortSignal | null | undefined
  context.mock.method(globalThis, 'fetch', (url: RequestInfo | URL, options?: RequestInit) => {
    if (String(url).includes('action=company.create')) { oldSignal = options?.signal; return response.promise }
    return Promise.resolve(Response.json([{ ...auditEvent, action: 'user.activate' }]))
  })
  const oldOptions = adminAuditOptions({ role: 'org_admin', action: 'company.create' })
  const observer = new QueryObserver(client, oldOptions)
  context.after(observer.subscribe(() => undefined))
  assert.equal(oldSignal?.aborted, false)
  // When
  observer.setOptions(adminAuditOptions({ role: 'org_admin', action: 'user.activate' }))
  assert.equal(observer.getCurrentResult().data, undefined)
  const result = await observer.refetch()
  response.resolve(Response.json([auditEvent]))
  // Then
  assert.equal(oldSignal?.aborted, true)
  assert.equal(result.data?.[0]?.action, 'user.activate')
  assert.equal(client.getQueryData(oldOptions.queryKey), undefined)
})

for (const transition of ['logout', 'same-token', 'different-token'] as const) {
  for (const status of [200, 401, 403]) {
    test(`late audit response ${status} cannot cross ${transition} authentication generations`, async (context) => {
      // Given
      const session = ownAuditSession(context)
      const body = Promise.withResolvers<string>()
      const response = new DeferredBodyResponse(status, body.promise)
      context.mock.method(globalThis, 'fetch', async () => response)
      const pending = fetchAdminAudit({ role: 'org_admin' })
      const rejected = assert.rejects(pending, StaleSessionError)
      await response.bodyStarted.promise
      // When
      if (transition === 'logout') session.logout()
      else session.replace(transition === 'same-token' ? 'synthetic-audit-token' : 'replacement-token')
      const current = session.capture()
      body.resolve(JSON.stringify(status === 200 ? [auditEvent] : { detail: 'Retired error' }))
      await rejected
      // Then
      assert.equal(session.isCurrent(current), true)
      assert.equal(current.token, transition === 'logout' ? null : transition === 'same-token' ? 'synthetic-audit-token' : 'replacement-token')
    })
  }
}

test('retiring the query owner aborts and clears audit cache before a replacement session loads', async (context) => {
  // Given
  const session = ownAuditSession(context)
  const previous = createSessionQueryScope(session.capture())
  context.after(() => previous.client.clear())
  context.after(ownQueryClient(session, previous.owner, previous.client))
  const response = Promise.withResolvers<Response>()
  let signal: AbortSignal | null | undefined
  context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, options?: RequestInit) => { signal = options?.signal; return response.promise })
  const options = adminAuditOptions({ role: 'org_admin' })
  const pending = previous.client.fetchQuery(options)
  const rejected = assert.rejects(pending, isCancelledError)
  // When
  session.replace('replacement-token')
  const current = createSessionQueryScope(session.capture())
  context.after(() => current.client.clear())
  current.client.setQueryData(options.queryKey, [adminAuditLogSchema.parse({ ...auditEvent, action: 'replacement.action' })])
  response.resolve(Response.json([auditEvent]))
  await rejected
  // Then
  assert.equal(signal?.aborted, true)
  assert.equal(previous.client.getQueryCache().getAll().length, 0)
  assert.equal(current.client.getQueryData(options.queryKey)?.[0]?.action, 'replacement.action')
})

test('page identity separates role, filters, limit, opaque cursor and refreshed root', () => {
  // Given
  const scopes: readonly AdminAuditPageScope[] = [
    { role: 'org_admin' }, { role: 'platform_admin' },
    { role: 'platform_admin', tenant_id: tenantId }, { role: 'platform_admin', actor_id: actorId },
    { role: 'platform_admin', action: 'user.activate' }, { role: 'platform_admin', limit: 500 },
    { role: 'org_admin', cursor: 'opaque-a' }, { role: 'org_admin', cursor: 'opaque-b' },
  ]
  // When
  const identities = scopes.map((scope) => JSON.stringify(adminAuditPageOptions(scope).queryKey))
  // Then
  assert.equal(new Set(identities).size, scopes.length)
  assert.deepEqual(adminAuditPageOptions({ role: 'platform_admin', action: ' user.activate ' }).queryKey,
    adminAuditPageOptions({ role: 'platform_admin', action: 'user.activate', limit: 100 }).queryKey)
  assert.notDeepEqual(adminAuditPageOptions({ role: 'org_admin' }, 1).queryKey, adminAuditPageOptions({ role: 'org_admin' }).queryKey)
  assert.notEqual(adminAuditPageOptions({ role: 'org_admin' }).queryKey[0], adminAuditOptions({ role: 'org_admin' }).queryKey[0])
})

for (const next of ['filter', 'back', 'refresh'] as const) {
  test(`${next} abandons an in-flight older page without leaking its records or continuation`, async (context) => {
    // Given
    const session = ownAuditSession(context)
    const { client } = createSessionQueryScope(session.capture())
    context.after(() => client.clear())
    const response = Promise.withResolvers<Response>()
    let signal: AbortSignal | null | undefined
    context.mock.method(globalThis, 'fetch', (url: RequestInfo | URL, options?: RequestInit) => {
      if (String(url).includes('cursor=old')) { signal = options?.signal; return response.promise }
      return Promise.resolve(Response.json({ items: [{ ...auditEvent, action: 'current.page' }], next_cursor: 'current-next' }))
    })
    const old = adminAuditPageOptions({ role: 'org_admin', cursor: 'old' })
    const observer = new QueryObserver(client, old)
    context.after(observer.subscribe(() => undefined))
    // When
    observer.setOptions(adminAuditPageOptions({ role: 'org_admin', ...(next === 'filter' ? { action: 'new.filter' } : {}) }, next === 'refresh' ? 1 : 0))
    assert.equal(observer.getCurrentResult().data, undefined)
    const result = await observer.refetch()
    response.resolve(Response.json({ items: [auditEvent], next_cursor: 'retired-next' }))
    // Then
    assert.equal(signal?.aborted, true)
    assert.equal(result.data?.items[0]?.action, 'current.page')
    assert.equal(result.data?.next_cursor, 'current-next')
    assert.equal(client.getQueryData(old.queryKey), undefined)
  })
}

for (const transition of ['logout', 'same-token', 'different-token'] as const) for (const status of [200, 401, 403]) {
  test(`late older-page ${status} cannot cross ${transition} session ownership`, async (context) => {
    // Given
    const session = ownAuditSession(context)
    const previous = createSessionQueryScope(session.capture())
    context.after(() => previous.client.clear())
    context.after(ownQueryClient(session, previous.owner, previous.client))
    const body = Promise.withResolvers<string>()
    const response = new DeferredBodyResponse(status, body.promise)
    context.mock.method(globalThis, 'fetch', async () => response)
    const options = adminAuditPageOptions({ role: 'org_admin', cursor: 'old-owner' })
    const pending = previous.client.fetchQuery(options)
    const rejected = assert.rejects(pending, isCancelledError)
    await response.bodyStarted.promise
    // When
    if (transition === 'logout') session.logout()
    else session.replace(transition === 'same-token' ? 'synthetic-audit-token' : 'replacement-token')
    const current = createSessionQueryScope(session.capture())
    context.after(() => current.client.clear())
    current.client.setQueryData(options.queryKey, adminAuditPageSchema.parse({ items: [{ ...auditEvent, action: 'replacement.page' }], next_cursor: null }))
    body.resolve(JSON.stringify(status === 200 ? { items: [auditEvent], next_cursor: 'retired-next' } : { detail: 'Retired error' }))
    await rejected
    // Then
    assert.equal(previous.client.getQueryCache().getAll().length, 0)
    assert.equal(session.isCurrent(current.owner), true)
    assert.equal(current.client.getQueryData(options.queryKey)?.items[0]?.action, 'replacement.page')
    assert.equal(current.client.getQueryData(options.queryKey)?.next_cursor, null)
  })
}

test('direct older-page body completion is fenced even without a query observer', async (context) => {
  // Given
  const session = ownAuditSession(context)
  const response = new DeferredBodyResponse(200, Promise.resolve(JSON.stringify({ items: [auditEvent], next_cursor: null })))
  context.mock.method(globalThis, 'fetch', async () => { session.replace('synthetic-audit-token'); return response })
  // When / Then
  await assert.rejects(fetchAdminAuditPage({ role: 'org_admin', cursor: 'old-owner' }), StaleSessionError)
})
