import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { renameDepartment, type DepartmentsResponse } from '../src/lib/departments-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { renameDepartmentOptions } from '../src/pages/users-department-mutations.ts'
import { createFixture } from './helpers.ts'

const original = { id: 'department/a +?', tenant_id: 'tenant &a', name: 'Operations', created_at: '2026-10-18T00:00:00Z' }
const renamed = { ...original, name: 'Customer operations' }
const input = { tenantId: original.tenant_id, departmentId: original.id, name: renamed.name }
const queryKey = ['admin-departments', original.tenant_id]
const initial: DepartmentsResponse = {
  departments: [original, { ...original, id: 'engineering', name: 'Engineering' }],
  assignments: [{ tenant_id: original.tenant_id, user_id: 'member-a', department_id: original.id }],
}

test('rename sends an encoded scoped PATCH and parses the department response', async (t) => {
  // Given
  let requestedUrl: RequestInfo | URL | undefined
  let request: RequestInit | undefined
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL, options?: RequestInit) => {
    requestedUrl = url
    request = options
    return Response.json({ ...renamed, ignored: 'not a department field' })
  })
  // When
  const result = await renameDepartment(input.tenantId, input.departmentId, { name: input.name })
  // Then
  assert.equal(requestedUrl, '/api/departments/department%2Fa%20%2B%3F?tenant_id=tenant+%26a')
  assert.equal(request?.method, 'PATCH')
  assert.deepEqual(JSON.parse(String(request?.body)), { name: renamed.name })
  assert.deepEqual(result, renamed)
  assert.equal(Object.isFrozen(result), true)
})

test('rename rejects a malformed department response', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ id: original.id, name: renamed.name }))
  // When
  const pending = renameDepartment(input.tenantId, input.departmentId, { name: input.name })
  // Then
  await assert.rejects(pending, z.ZodError)
})

test('rename preserves duplicate-name errors for inline field feedback', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Department name already exists' }, { status: 409 }))
  // When
  const pending = renameDepartment(input.tenantId, input.departmentId, { name: input.name })
  // Then
  await assert.rejects(pending, (error) => error instanceof ApiError && error.status === 409)
})

test('rename replaces the cached department in place and invalidates only the exact tenant list', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(queryKey, initial)
  const unrelatedKeys = [['admin-departments', 'tenant-b'], [...queryKey, 'detail'], ['admin-users', original.tenant_id]]
  for (const key of unrelatedKeys) client.setQueryData(key, initial)
  const before = client.getQueryData<DepartmentsResponse>(queryKey)
  t.mock.method(globalThis, 'fetch', async () => Response.json(renamed))
  const observer = new MutationObserver(client, renameDepartmentOptions(client))
  // When
  await observer.mutate(input)
  // Then
  const result = client.getQueryData<DepartmentsResponse>(queryKey)
  assert.deepEqual(result?.departments, [renamed, initial.departments[1]])
  assert.strictEqual(result?.assignments, before?.assignments)
  assert.equal(client.getQueryState(queryKey)?.isInvalidated, true)
  for (const key of unrelatedKeys) {
    assert.deepEqual(client.getQueryData(key), initial)
    assert.equal(client.getQueryState(key)?.isInvalidated, false)
  }
})

test('a saved rename completes while its list reconciliation is pending', { timeout: 1000 }, async (t) => {
  // Given
  const client = new QueryClient()
  const refreshed = Promise.withResolvers<DepartmentsResponse>()
  client.setQueryData(queryKey, initial)
  const query = new QueryObserver(client, { queryKey, queryFn: () => refreshed.promise, staleTime: Infinity })
  const unsubscribe = query.subscribe(() => {})
  t.after(() => { refreshed.resolve(initial); unsubscribe(); client.clear() })
  t.mock.method(globalThis, 'fetch', async () => Response.json(renamed))
  const observer = new MutationObserver(client, renameDepartmentOptions(client))
  // When
  await observer.mutate(input)
  // Then
  assert.equal(observer.getCurrentResult().status, 'success')
  assert.equal(query.getCurrentResult().isFetching, true)
  assert.equal(client.getQueryData<DepartmentsResponse>(queryKey)?.departments[0]?.name, renamed.name)
})

test('a rejected rename leaves the cached name and assignments unchanged', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(queryKey, initial)
  const before = client.getQueryData(queryKey)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  const observer = new MutationObserver(client, renameDepartmentOptions(client))
  // When
  const pending = observer.mutate(input)
  // Then
  await assert.rejects(pending, (error) => error instanceof ApiError && error.status === 503)
  assert.strictEqual(client.getQueryData(queryKey), before)
  assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
})

test('a late rename cannot rewrite or invalidate a retired session cache', async (t) => {
  // Given
  const { policy } = createFixture()
  const owner = policy.capture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(queryKey, initial)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const observer = new MutationObserver(client, ownMutationOptions(policy, owner, renameDepartmentOptions(client)))
  const pending = observer.mutate(input)
  const rejected = assert.rejects(pending, StaleSessionError)
  await started.promise
  // When
  policy.replace('token-b')
  response.resolve(Response.json(renamed))
  // Then
  await rejected
  assert.deepEqual(client.getQueryData(queryKey), initial)
  assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
})
