import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { groupDepartmentAssignments, listDepartments, renameDepartment, setUserDepartments, type DepartmentsResponse } from '../src/lib/departments-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { replaceUserDepartmentsOptions, renameDepartmentOptions } from '../src/pages/users-department-mutations.ts'
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

const memberId = 'member/a +?'
const memberships: DepartmentsResponse = {
  ...initial,
  assignments: [
    { tenant_id: original.tenant_id, user_id: memberId, department_id: original.id },
    { tenant_id: original.tenant_id, user_id: 'member-b', department_id: 'engineering' },
    { tenant_id: original.tenant_id, user_id: memberId, department_id: 'engineering' },
  ],
}

test('loading departments groups every membership row without losing earlier selections', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json(memberships))
  // When
  const grouped = groupDepartmentAssignments((await listDepartments(original.tenant_id)).assignments)
  // Then
  assert.deepEqual(grouped.get(memberId), [original.id, 'engineering'])
  assert.deepEqual(grouped.get('member-b'), ['engineering'])
})

test('replace-set sends an encoded scoped plural PUT and parses the complete response', async (t) => {
  // Given
  const updated = { tenant_id: original.tenant_id, user_id: memberId, department_ids: ['engineering', original.id] }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _request?: RequestInit) => Response.json({ ...updated, ignored: true }))
  // When
  const result = await setUserDepartments(original.tenant_id, memberId, [original.id, 'engineering'])
  // Then
  const [url, request] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/users/member%2Fa%20%2B%3F/departments?tenant_id=tenant+%26a')
  assert.equal(request?.method, 'PUT')
  assert.deepEqual(JSON.parse(String(request?.body)), { department_ids: [original.id, 'engineering'] })
  assert.deepEqual(result, updated)
  assert.equal(Object.isFrozen(result.department_ids), true)
})

test('replace-set rejects a malformed membership response', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ user_id: memberId, tenant_id: original.tenant_id, department_ids: [null] }))
  // When
  const pending = setUserDepartments(original.tenant_id, memberId, [])
  // Then
  await assert.rejects(pending, z.ZodError)
})

for (const departmentIds of [['engineering', original.id], ['engineering'], []]) {
  test(`replace-set reconciles ${departmentIds.length} memberships and only invalidates exact owned keys`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const ownedKeys = [queryKey, ['admin-users', original.tenant_id], ['admin-user', memberId]]
    const unrelatedKeys = [['admin-departments', 'tenant-b'], [...queryKey, 'detail'], ['admin-users', 'tenant-b'], ['admin-user', 'member-b']]
    for (const key of [...ownedKeys, ...unrelatedKeys]) client.setQueryData(key, memberships)
    const updated = { tenant_id: original.tenant_id, user_id: memberId, department_ids: departmentIds }
    t.mock.method(globalThis, 'fetch', async () => Response.json(updated))
    const observer = new MutationObserver(client, replaceUserDepartmentsOptions(client))
    // When
    await observer.mutate({ tenantId: original.tenant_id, userId: memberId, departmentIds: ['ignored-request-id'] })
    // Then
    const result = client.getQueryData<DepartmentsResponse>(queryKey)
    assert.deepEqual(result?.assignments, [memberships.assignments[1], ...departmentIds.map((department_id) => ({ user_id: memberId, tenant_id: original.tenant_id, department_id }))])
    assert.deepEqual(result?.departments, memberships.departments)
    for (const key of ownedKeys) assert.equal(client.getQueryState(key)?.isInvalidated, true)
    for (const key of unrelatedKeys) {
      assert.deepEqual(client.getQueryData(key), memberships)
      assert.equal(client.getQueryState(key)?.isInvalidated, false)
    }
  })
}

test('a rejected replace-set preserves every cached membership for inline error recovery', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(queryKey, memberships)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Department belongs to another tenant' }, { status: 403 }))
  const observer = new MutationObserver(client, replaceUserDepartmentsOptions(client))
  // When
  const pending = observer.mutate({ tenantId: original.tenant_id, userId: memberId, departmentIds: [] })
  // Then
  await assert.rejects(pending, (error) => error instanceof ApiError && error.status === 403)
  assert.deepEqual(client.getQueryData(queryKey), memberships)
  assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
})

for (const status of [200, 403]) {
  test(`a late replace-set ${status} response cannot affect a replacement session`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(queryKey, memberships)
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), replaceUserDepartmentsOptions(client)))
    const rejected = assert.rejects(observer.mutate({ tenantId: original.tenant_id, userId: memberId, departmentIds: [] }), StaleSessionError)
    await started.promise
    // When
    policy.replace('token-b')
    response.resolve(Response.json({ tenant_id: original.tenant_id, user_id: memberId, department_ids: [] }, { status }))
    // Then
    await rejected
    assert.deepEqual(client.getQueryData(queryKey), memberships)
    assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
  })
}
