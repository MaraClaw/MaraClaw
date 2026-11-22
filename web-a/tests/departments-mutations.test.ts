import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { renameDepartment, setUserDepartments, type DepartmentAssignments, type DepartmentsResponse } from '../src/lib/departments-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { departmentAssignmentsOptions } from '../src/pages/users-department-assignment-options.ts'
import { createDepartmentOptions, replaceUserDepartmentsOptions, renameDepartmentOptions } from '../src/pages/users-department-mutations.ts'
import { createFixture } from './helpers.ts'

const original = { id: 'department/a +?', tenant_id: 'tenant &a', name: 'Operations', description: '  Escalations\nBilling  ', created_at: '2026-10-18T00:00:00Z' }
const renamed = { ...original, name: 'Customer operations' }
const input = { tenantId: original.tenant_id, departmentId: original.id, name: renamed.name }
const queryKey = ['admin-departments', original.tenant_id]
const initial: DepartmentsResponse = {
  departments: [original, { ...original, id: 'engineering', name: 'Engineering' }],
}

for (const description of [undefined, '', '  New responsibilities\nEscalations  ']) {
  test(`create adapter carries ${JSON.stringify(description)} into the wire and owned directory`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(queryKey, initial)
    const payload = description === undefined ? { name: 'Support' } : { name: 'Support', description }
    const created = { ...original, id: 'support', ...payload, description: description ?? '' }
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(created))
    const { policy } = createFixture()
    const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), createDepartmentOptions(client)))
    // When
    await observer.mutate({ tenantId: original.tenant_id, ...payload })
    // Then
    assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), payload)
    assert.deepEqual(client.getQueryData<DepartmentsResponse>(queryKey)?.departments.at(-1), created)
  })
}

test('late create description cannot populate a replacement session directory', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(queryKey, initial)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), createDepartmentOptions(client)))
  const rejected = assert.rejects(observer.mutate({ tenantId: original.tenant_id, name: 'Support', description: '  Private responsibilities  ' }), StaleSessionError)
  await started.promise
  // When
  policy.replace('token-b')
  response.resolve(Response.json({ ...original, id: 'support', description: '  Private responsibilities  ' }))
  // Then
  await rejected
  assert.deepEqual(client.getQueryData(queryKey), initial)
  assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
})

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

for (const description of ['', '  Revised billing ownership\n\nOnboarding  ']) {
  test(`same-name edit forwards exact description ${JSON.stringify(description)} and updates owned cache`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(queryKey, initial)
    const updated = { ...original, description }
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(updated))
    const observer = new MutationObserver(client, renameDepartmentOptions(client))
    // When
    await observer.mutate({ tenantId: original.tenant_id, departmentId: original.id, name: original.name, description })
    // Then
    assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), { name: original.name, description })
    assert.deepEqual(client.getQueryData<DepartmentsResponse>(queryKey)?.departments[0], updated)
  })
}

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
  const unrelatedKeys = [['admin-departments', 'tenant-b'], [...queryKey, 'assignments', ['member-a']], ['admin-users', original.tenant_id]]
  for (const key of unrelatedKeys) client.setQueryData(key, initial)
  t.mock.method(globalThis, 'fetch', async () => Response.json(renamed))
  const observer = new MutationObserver(client, renameDepartmentOptions(client))
  // When
  await observer.mutate(input)
  // Then
  const result = client.getQueryData<DepartmentsResponse>(queryKey)
  assert.deepEqual(result?.departments, [renamed, initial.departments[1]])
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
const memberships: DepartmentAssignments = [
  { tenant_id: original.tenant_id, user_id: memberId, department_id: original.id },
  { tenant_id: original.tenant_id, user_id: 'member-b', department_id: 'engineering' },
  { tenant_id: original.tenant_id, user_id: memberId, department_id: 'engineering' },
]
const assignmentPage = [...queryKey, 'assignments', [memberId, 'member-b'].sort()]

test('assignment query scopes and groups membership rows and shares a cache entry regardless of input order', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL) => Response.json(memberships))
  const options = departmentAssignmentsOptions(original.tenant_id, ['member-b', memberId, memberId])
  const reversed = departmentAssignmentsOptions(original.tenant_id, [memberId, 'member-b'])
  const observer = new QueryObserver(client, options)
  const empty = new QueryObserver(client, departmentAssignmentsOptions(original.tenant_id, []))
  const unsubscribe = empty.subscribe(() => {})
  t.after(unsubscribe)

  const { data: grouped } = await observer.refetch()

  assert.deepEqual(options.queryKey, assignmentPage)
  assert.deepEqual(reversed.queryKey, options.queryKey)
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/departments/assignments?tenant_id=tenant+%26a&user_id=member-b&user_id=member%2Fa+%2B%3F')
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(empty.getCurrentResult().fetchStatus, 'idle')
  assert.deepEqual(grouped?.get(memberId), [original.id, 'engineering'])
  assert.deepEqual(grouped?.get('member-b'), ['engineering'])
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
  test(`replace-set reconciles ${departmentIds.length} memberships only on pages containing the subject`, async (t) => {
    const client = new QueryClient()
    t.after(() => client.clear())
    const overlap = [...queryKey, 'assignments', [memberId]]
    const otherPage = [...queryKey, 'assignments', ['member-b']]
    const otherTenantPage = ['admin-departments', 'tenant-b', 'assignments', [memberId]]
    const subjectDetail = ['admin-user', memberId]
    const unrelated = [otherPage, otherTenantPage, ['admin-user', 'member-b'], ['admin-users', original.tenant_id, 'page']]
    const otherMembership = memberships.filter((row) => row.user_id !== memberId)
    client.setQueryData(queryKey, initial)
    client.setQueryData(assignmentPage, memberships)
    client.setQueryData(overlap, []) // The page contains the subject even if its cached response has no rows.
    client.setQueryData(subjectDetail, { id: memberId })
    for (const key of unrelated) client.setQueryData(key, otherMembership)
    const updated = { tenant_id: original.tenant_id, user_id: memberId, department_ids: departmentIds }
    t.mock.method(globalThis, 'fetch', async () => Response.json(updated))
    const observer = new MutationObserver(client, replaceUserDepartmentsOptions(client))

    await observer.mutate({ tenantId: original.tenant_id, userId: memberId, departmentIds: ['ignored-request-id'] })

    const updatedRows = departmentIds.map((department_id) => ({ user_id: memberId, tenant_id: original.tenant_id, department_id }))
    assert.deepEqual(client.getQueryData(assignmentPage), [...otherMembership, ...updatedRows])
    assert.deepEqual(client.getQueryData(overlap), updatedRows)
    assert.deepEqual(client.getQueryData(queryKey), initial)
    assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
    assert.equal(client.getQueryState(subjectDetail)?.isInvalidated, true)
    for (const key of unrelated) {
      assert.deepEqual(client.getQueryData(key), otherMembership)
      assert.equal(client.getQueryState(key)?.isInvalidated, false)
    }
  })
}

test('a rejected replace-set preserves every cached membership for inline error recovery', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(assignmentPage, memberships)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Department belongs to another tenant' }, { status: 403 }))
  const observer = new MutationObserver(client, replaceUserDepartmentsOptions(client))
  // When
  const pending = observer.mutate({ tenantId: original.tenant_id, userId: memberId, departmentIds: [] })
  // Then
  await assert.rejects(pending, (error) => error instanceof ApiError && error.status === 403)
  assert.deepEqual(client.getQueryData(assignmentPage), memberships)
  assert.equal(client.getQueryState(assignmentPage)?.isInvalidated, false)
})

for (const status of [200, 403]) {
  test(`a late replace-set ${status} response cannot affect a replacement session`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(assignmentPage, memberships)
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
    assert.deepEqual(client.getQueryData(assignmentPage), memberships)
    assert.equal(client.getQueryState(assignmentPage)?.isInvalidated, false)
  })
}
