import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { deleteDepartment, previewDepartmentDeletion, type DepartmentsResponse } from '../src/lib/departments-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { deleteDepartmentOptions } from '../src/pages/users-department-mutations.ts'
import { createFixture } from './helpers.ts'

const department = { id: 'department/a +?', tenant_id: 'tenant &a', name: 'Operations', created_at: '2026-10-18T00:00:00Z' }
const input = { tenantId: department.tenant_id, departmentId: department.id }
const queryKey = ['admin-departments', input.tenantId]
const directory: DepartmentsResponse = {
  departments: [department, { ...department, id: 'engineering', name: 'Engineering' }],
  assignments: [
    { tenant_id: input.tenantId, user_id: 'member-a', department_id: department.id },
    { tenant_id: input.tenantId, user_id: 'member-b', department_id: department.id },
    { tenant_id: input.tenantId, user_id: 'member-a', department_id: 'engineering' },
  ],
}

test('deletion preview parses association counts and forwards encoded scope and cancellation', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ membership_count: 0, agent_grant_count: 12 }))
  // When
  const result = await previewDepartmentDeletion(input.tenantId, input.departmentId, controller.signal)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/departments/department%2Fa%20%2B%3F/deletion-preview?tenant_id=tenant+%26a')
  assert.equal(options?.signal, controller.signal)
  assert.deepEqual(result, { membership_count: 0, agent_grant_count: 12 })
})

for (const body of [{}, { membership_count: -1, agent_grant_count: 0 }, { membership_count: 1, agent_grant_count: 1.5 }, { membership_count: '2', agent_grant_count: 0 }]) {
  test(`deletion preview rejects malformed counts ${JSON.stringify(body)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(previewDepartmentDeletion(input.tenantId, input.departmentId), z.ZodError)
  })
}

for (const status of [403, 404, 503]) {
  test(`deletion preview preserves failure ${status} for retry`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status }))
    // When / Then
    await assert.rejects(previewDepartmentDeletion(input.tenantId, input.departmentId), (error) => error instanceof ApiError && error.status === status)
  })
}

test('delete sends the encoded tenant-scoped DELETE and accepts an empty 204', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => new Response(null, { status: 204 }))
  // When
  const result = await deleteDepartment(input.tenantId, input.departmentId)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/departments/department%2Fa%20%2B%3F?tenant_id=tenant+%26a')
  assert.equal(options?.method, 'DELETE')
  assert.equal(options?.body, undefined)
  assert.equal(result, undefined)
})

test('successful deletion prunes only the owned directory and refreshes paged users and affected details', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const ownedKeys = [queryKey, ['admin-users', input.tenantId], ['admin-users', input.tenantId, 'page', 'search', 1, 0], ['admin-user', 'member-a'], ['admin-user', 'member-b']]
  const unrelatedKeys = [['admin-departments', 'tenant-b'], [...queryKey, 'detail'], ['admin-users', 'tenant-b', 'page'], ['admin-user', 'member-c']]
  for (const key of [...ownedKeys, ...unrelatedKeys]) client.setQueryData(key, directory)
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  const { policy } = createFixture()
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), deleteDepartmentOptions(client)))
  // When
  await observer.mutate(input)
  // Then
  assert.deepEqual(client.getQueryData(queryKey), { departments: [directory.departments[1]], assignments: [directory.assignments[2]] })
  for (const key of ownedKeys) assert.equal(client.getQueryState(key)?.isInvalidated, true)
  for (const key of unrelatedKeys) {
    assert.deepEqual(client.getQueryData(key), directory)
    assert.equal(client.getQueryState(key)?.isInvalidated, false)
  }
})

test('confirmed deletion finishes while directory reconciliation remains pending', { timeout: 1000 }, async (t) => {
  // Given
  const client = new QueryClient()
  const refreshed = Promise.withResolvers<DepartmentsResponse>()
  client.setQueryData(queryKey, directory)
  const query = new QueryObserver(client, { queryKey, queryFn: () => refreshed.promise, staleTime: Infinity })
  const unsubscribe = query.subscribe(() => {})
  t.after(() => { refreshed.resolve(directory); unsubscribe(); client.clear() })
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  const observer = new MutationObserver(client, deleteDepartmentOptions(client))
  // When
  await observer.mutate(input)
  // Then
  assert.equal(observer.getCurrentResult().status, 'success')
  assert.equal(query.getCurrentResult().isFetching, true)
  assert.equal(client.getQueryData<DepartmentsResponse>(queryKey)?.departments.length, 1)
})

for (const status of [403, 404, 409, 503]) {
  test(`rejected deletion ${status} preserves the directory and assignments for recovery`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(queryKey, directory)
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Department could not be deleted' }, { status }))
    const observer = new MutationObserver(client, deleteDepartmentOptions(client))
    // When
    const pending = observer.mutate(input)
    // Then
    await assert.rejects(pending, (error) => error instanceof ApiError && error.status === status)
    assert.deepEqual(client.getQueryData(queryKey), directory)
    assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
  })
}

for (const status of [204, 403]) {
  test(`late deletion ${status} cannot update or invalidate a same-token replacement session`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const keys = [queryKey, ['admin-users', input.tenantId, 'page'], ['admin-user', 'member-a']]
    for (const key of keys) client.setQueryData(key, directory)
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), deleteDepartmentOptions(client)))
    const rejected = assert.rejects(observer.mutate(input), StaleSessionError)
    await started.promise
    // When
    policy.replace('token-a')
    response.resolve(status === 204 ? new Response(null, { status }) : Response.json({ detail: 'Forbidden' }, { status }))
    // Then
    await rejected
    for (const key of keys) {
      assert.deepEqual(client.getQueryData(key), directory)
      assert.equal(client.getQueryState(key)?.isInvalidated, false)
    }
  })
}
