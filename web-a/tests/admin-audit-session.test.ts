import assert from 'node:assert/strict'
import test from 'node:test'
import { QueryObserver, isCancelledError } from '@tanstack/react-query'

import { adminAuditOptions } from '../src/hooks/admin-audit-options.ts'
import { createSessionQueryScope, ownQueryClient } from '../src/hooks/session-query-client.ts'
import { adminAuditLogSchema, fetchAdminAudit, type AdminAuditScope } from '../src/lib/admin-audit-api.ts'
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
