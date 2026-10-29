import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { mergeDepartment, previewDepartmentMerge, type DepartmentsResponse } from '../src/lib/departments-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { mergeDepartmentOptions } from '../src/pages/users-department-mutations.ts'
import { createFixture } from './helpers.ts'

const source = { id: '10000000-0000-4000-8000-000000000001', tenant_id: '20000000-0000-4000-8000-000000000001', name: 'Operations', description: '  Escalations\nBilling  ', created_at: '2026-10-18T00:00:00Z' }
const destination = { ...source, id: '10000000-0000-4000-8000-000000000002', name: 'Engineering', description: '  Onboarding\nProduct support  ' }
const other = { ...source, id: '10000000-0000-4000-8000-000000000003', name: 'Finance' }
const confirmationToken = `v1:${'6'.repeat(64)}`
const input = { tenantId: source.tenant_id, sourceId: source.id, destinationId: destination.id, confirmationToken }
const queryKey = ['admin-departments', input.tenantId]
const assignment = (user_id: string, department_id: string) => ({ user_id, department_id, tenant_id: source.tenant_id })
const directory: DepartmentsResponse = {
  departments: [source, destination, other],
  assignments: [assignment('overlap', source.id), assignment('overlap', destination.id), assignment('source-only', source.id), assignment('destination-only', destination.id), assignment('source-only', other.id), assignment('unrelated', other.id)],
}

for (const field of ['source', 'destination'] as const) {
  test(`preview rejects null ${field} description instead of hiding reviewed metadata`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ...result, [field]: { ...result[field], description: null } }))
    // When / Then
    await assert.rejects(previewDepartmentMerge(input), z.ZodError)
  })
}
const result = {
  confirmation_token: confirmationToken,
  source, destination: { ...destination, name: 'Current engineering' },
  source_membership_count: 2, destination_membership_count: 2, overlapping_membership_count: 1,
  source_agent_grant_count: 3, destination_agent_grant_count: 4, overlapping_agent_grant_count: 2,
}

test('confirmed merge sends the exact reviewed token in its single write', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(result))
  const observer = new MutationObserver(client, mergeDepartmentOptions(client))
  // When
  await observer.mutate(input)
  // Then
  const [, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.deepEqual(JSON.parse(String(options?.body)), { destination_id: destination.id, confirmation_token: confirmationToken })
  assert.equal(fetch.mock.callCount(), 1)
})

for (const confirmation_token of [undefined, null, 'v1:short', `v1:${'A'.repeat(64)}`, `v2:${'1'.repeat(64)}`]) {
  for (const endpoint of [previewDepartmentMerge, mergeDepartment]) {
    test(`${endpoint.name} rejects a missing or malformed preview confirmation ${confirmation_token}`, async (t) => {
      // Given
      t.mock.method(globalThis, 'fetch', async () => Response.json({ ...result, confirmation_token }))
      // When / Then
      await assert.rejects(endpoint(input), z.ZodError)
    })
  }
}

test('merge unions overlapping memberships and refreshes source and destination members without touching another tenant', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const owned = [queryKey, ['admin-users', input.tenantId, 'page', 'search'], ...['overlap', 'source-only', 'destination-only'].map((user) => ['admin-user', user])]
  const unrelated = [['admin-departments', 'other-tenant'], ['admin-users', 'other-tenant', 'page'], ['admin-user', 'unrelated'], [...queryKey, 'detail']]
  for (const key of [...owned, ...unrelated]) client.setQueryData(key, directory)
  t.mock.method(globalThis, 'fetch', async () => Response.json(result))
  const { policy } = createFixture()
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), mergeDepartmentOptions(client)))
  // When
  await observer.mutate(input)
  // Then
  assert.deepEqual(client.getQueryData(queryKey), {
    departments: [result.destination, other],
    assignments: [assignment('overlap', destination.id), assignment('source-only', destination.id), assignment('destination-only', destination.id), assignment('source-only', other.id), assignment('unrelated', other.id)],
  })
  for (const key of owned) assert.equal(client.getQueryState(key)?.isInvalidated, true)
  for (const key of unrelated) {
    assert.deepEqual(client.getQueryData(key), directory)
    assert.equal(client.getQueryState(key)?.isInvalidated, false)
  }
})

test('confirmed merge finishes without waiting for directory reconciliation', { timeout: 1000 }, async (t) => {
  // Given
  const client = new QueryClient()
  client.setQueryData(queryKey, directory)
  const refreshed = Promise.withResolvers<DepartmentsResponse>()
  const query = new QueryObserver(client, { queryKey, queryFn: () => refreshed.promise, staleTime: Infinity })
  const unsubscribe = query.subscribe(() => {})
  t.after(() => { refreshed.resolve(directory); unsubscribe(); client.clear() })
  t.mock.method(globalThis, 'fetch', async () => Response.json(result))
  const observer = new MutationObserver(client, mergeDepartmentOptions(client))
  // When
  await observer.mutate(input)
  // Then
  assert.equal(observer.getCurrentResult().status, 'success')
  assert.equal(query.getCurrentResult().isFetching, true)
  assert.equal(client.getQueryData<DepartmentsResponse>(queryKey)?.departments.some((item) => item.id === source.id), false)
})

for (const status of [403, 404, 409, 422, 503]) {
  test(`merge failure ${status} preserves memberships and does not automatically repeat the write`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(queryKey, directory)
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Merge rejected' }, { status }))
    const observer = new MutationObserver(client, mergeDepartmentOptions(client))
    // When
    const pending = observer.mutate(input)
    // Then
    await assert.rejects(pending, (error) => error instanceof ApiError && error.status === status)
    assert.deepEqual(client.getQueryData(queryKey), directory)
    assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

for (const status of [200, 403, 409]) {
  test(`late merge ${status} cannot affect a same-token replacement session`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const keys = [queryKey, ['admin-users', input.tenantId, 'page'], ['admin-user', 'overlap']]
    for (const key of keys) client.setQueryData(key, directory)
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), mergeDepartmentOptions(client)))
    const rejected = assert.rejects(observer.mutate(input), StaleSessionError)
    await started.promise
    // When
    policy.replace('token-a')
    response.resolve(Response.json(status === 200 ? result : { detail: status === 409 ? { code: 'department_merge_preview_changed' } : 'Denied' }, { status }))
    // Then
    await rejected
    for (const key of keys) {
      assert.deepEqual(client.getQueryData(key), directory)
      assert.equal(client.getQueryState(key)?.isInvalidated, false)
    }
  })
}
