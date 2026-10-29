import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { createDepartment, deleteDepartment, listDepartments, mergeDepartment, previewDepartmentDeletion, previewDepartmentMerge, type DepartmentsResponse } from '../src/lib/departments-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { deleteDepartmentOptions } from '../src/pages/users-department-mutations.ts'
import { createFixture } from './helpers.ts'

const department = { id: 'department/a +?', tenant_id: 'tenant &a', name: 'Operations', description: '  Escalations\nBilling questions  ', created_at: '2026-10-18T00:00:00Z' }
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

const mergeSelection = { tenantId: '86b8142b-31e6-44ab-a35e-e10998037030', sourceId: department.id, destinationId: '64c0b6ca-0312-42aa-996d-ff606043337a' }
const mergeResult = {
  confirmation_token: `v1:${'a'.repeat(64)}`,
  source: { ...department, id: '10000000-0000-4000-8000-000000000001', tenant_id: mergeSelection.tenantId },
  destination: { ...department, id: mergeSelection.destinationId, tenant_id: mergeSelection.tenantId, name: 'Engineering', description: '  Product support\nOnboarding  ' },
  source_membership_count: 2, destination_membership_count: 3, overlapping_membership_count: 1,
  source_agent_grant_count: 4, destination_agent_grant_count: 5, overlapping_agent_grant_count: 2,
}

test('list retains exact non-null department descriptions', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json(directory))
  // When
  const result = await listDepartments(input.tenantId)
  // Then
  assert.deepEqual(result, directory)
})

for (const description of [undefined, '', '  Billing\n\nEscalations  ']) {
  test(`create preserves ${JSON.stringify(description)} without breaking name-only requests`, async (t) => {
    // Given
    const payload = description === undefined ? { name: department.name } : { name: department.name, description }
    const created = { ...department, description: description ?? '' }
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(created))
    // When
    const result = await createDepartment(input.tenantId, payload)
    // Then
    assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), payload)
    assert.deepEqual(result, created)
  })
}

for (const description of [null, undefined, 42]) {
  test(`directory rejects malformed description ${description}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ...directory, departments: [{ ...department, description }] }))
    // When / Then
    await assert.rejects(listDepartments(input.tenantId), z.ZodError)
  })
}

test('merge preview validates all association counts and forwards the scoped selection and cancellation', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(mergeResult))
  // When
  const result = await previewDepartmentMerge(mergeSelection, controller.signal)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/departments/department%2Fa%20%2B%3F/merge-preview?tenant_id=${mergeSelection.tenantId}&destination_id=${mergeSelection.destinationId}`)
  assert.equal(options?.signal, controller.signal)
  assert.deepEqual(result, mergeResult)
})

test('merge applies with one atomic POST containing the destination and reviewed confirmation', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(mergeResult))
  // When
  const result = await mergeDepartment({ ...mergeSelection, confirmationToken: mergeResult.confirmation_token })
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/departments/department%2Fa%20%2B%3F/merge?tenant_id=${mergeSelection.tenantId}`)
  assert.equal(options?.method, 'POST')
  assert.deepEqual(JSON.parse(String(options?.body)), { destination_id: mergeSelection.destinationId, confirmation_token: mergeResult.confirmation_token })
  assert.deepEqual(result, mergeResult)
  assert.equal(fetch.mock.callCount(), 1)
})

for (const body of [{}, { ...mergeResult, source: null }, { ...mergeResult, destination: {} }, { ...mergeResult, source_membership_count: -1 }, { ...mergeResult, destination_membership_count: '3' }, { ...mergeResult, overlapping_membership_count: 0.5 }, { ...mergeResult, source_agent_grant_count: -1 }, { ...mergeResult, destination_agent_grant_count: null }, { ...mergeResult, overlapping_agent_grant_count: '2' }]) {
  test(`merge endpoints reject malformed response ${JSON.stringify(body)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(previewDepartmentMerge(mergeSelection), z.ZodError)
    await assert.rejects(mergeDepartment({ ...mergeSelection, confirmationToken: mergeResult.confirmation_token }), z.ZodError)
  })
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
