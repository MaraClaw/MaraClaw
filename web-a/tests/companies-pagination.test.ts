import assert from 'node:assert/strict'
import test from 'node:test'
import { InfiniteQueryObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { companiesPageOptions, companyOptions, companyStatsOptions } from '../src/hooks/company-options.ts'
import { getCompanyStats, listCompanies, listCompaniesPage, listCompanyOptions } from '../src/lib/companies-api.ts'
import { ApiError } from '../src/lib/http.ts'

const row = {
  id: 'alpha', name: 'Zebra Company', slug: 'zebra', is_active: true,
  sso_enabled: false, sso_domain: null, is_system: false, is_default_end_user_org: false,
  can_disable: true, created_at: null, user_count: 7, active_user_count: 5,
  inactive_user_count: 2, agent_count: 3, org_admin_email: null,
}

test('company page encodes search and opaque cursor and forwards cancellation', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({ items: [row], next_cursor: 'opaque/next +?=' }))
  const signal = new AbortController().signal
  // When
  const result = await listCompaniesPage({ search: ' %_Case+? ', cursor: 'opaque/cursor +?=' }, signal)
  // Then
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  const request = new URL(String(url), 'https://example.test')
  assert.equal(request.pathname, '/api/admin/companies/page')
  assert.deepEqual(Object.fromEntries(request.searchParams), { limit: '50', q: '%_Case+?', cursor: 'opaque/cursor +?=' })
  assert.strictEqual(init?.signal, signal)
  assert.deepEqual(result, { items: [row], next_cursor: 'opaque/next +?=' })
})

test('company pages append only on demand in server order and stop at null', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const cursors: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => {
    const request = new URL(String(url), 'https://example.test')
    assert.equal(request.searchParams.has('q'), false)
    const cursor = request.searchParams.get('cursor')
    cursors.push(cursor)
    return Response.json({ items: [{ ...row, id: cursor ? 'second' : 'first' }], next_cursor: cursor ? null : 'next' })
  })
  const observer = new InfiniteQueryObserver(client, companiesPageOptions(null, ''))
  t.after(observer.subscribe(() => undefined))
  await observer.refetch()
  assert.deepEqual(cursors, [null])
  // When
  const result = await observer.fetchNextPage()
  // Then
  assert.deepEqual(cursors, [null, 'next'])
  assert.deepEqual(result.data?.pages.flatMap(page => page.items.map(item => item.id)), ['first', 'second'])
  assert.equal(result.hasNextPage, false)
})

test('ranked company search terminates without a cursor', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [row], next_cursor: null }))
  const observer = new InfiniteQueryObserver(client, companiesPageOptions(null, 'Zebra'))
  // When
  const result = await observer.refetch()
  // Then
  assert.equal(observer.getCurrentResult().hasNextPage, false)
  assert.deepEqual(result.data?.pages.flatMap(page => page.items), [row])
})

for (const search of ['', 'ZEBRA', 'missing']) {
  test(`403 falls back to the own tenant and filters the terminal page: ${search}`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) =>
      String(url).startsWith('/api/tenants/') ? Response.json(row) : Response.json({ detail: 'Forbidden' }, { status: 403 }))
    // When
    const result = await client.fetchInfiniteQuery(companiesPageOptions('alpha', search))
    // Then
    assert.equal(fetch.mock.calls[1]?.arguments[0], '/api/tenants/alpha')
    assert.deepEqual(result.pages[0]?.items.map(item => item.id), search === 'missing' ? [] : ['alpha'])
    assert.equal(result.pages[0]?.next_cursor, null)
    assert.equal(result.pages[0]?.items[0]?.user_count, search === 'missing' ? undefined : 0)
  })
}

for (const [status, tenantId] of [[403, null], [503, 'alpha']] as const) {
  test(`company page preserves ${status} without an authorized own-tenant fallback`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Rejected' }, { status }))
    // When / Then
    await assert.rejects(client.fetchInfiniteQuery(companiesPageOptions(tenantId, '')), error => error instanceof ApiError && error.status === status)
    assert.equal(fetch.mock.calls.length, 1)
  })
}

test('picker options accept stats-free rows and preserve the complete server order', async (t) => {
  // Given
  const ordered = [{ id: 'z', name: 'Zebra' }, { id: 'a', name: 'Alpha' }, { id: 'own', name: 'Own' }]
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(ordered))
  // When
  const options = await listCompanyOptions()
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/companies/options')
  assert.deepEqual(options, ordered)
  assert.equal(options[0]?.id, 'z')
  assert.equal(options.some(company => company.id === 'own'), true)
  assert.equal(options.some(company => company.id === 'unknown'), false)
})

test('company detail reads one encoded stats endpoint while legacy listing remains a bare array', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) =>
    Response.json(String(url).endsWith('/stats') ? row : [row]))
  // When
  const detail = await getCompanyStats('alpha/+')
  const legacy = await listCompanies()
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/companies/alpha%2F%2B/stats')
  assert.equal(fetch.mock.calls[1]?.arguments[0], '/api/admin/companies')
  assert.deepEqual(detail, row)
  assert.deepEqual(legacy, [row])
})

for (const body of [[], { items: [], next_cursor: 42 }, { items: [{ ...row, user_count: '7' }], next_cursor: null }]) {
  test(`company page rejects malformed responses: ${JSON.stringify(body)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(listCompaniesPage({ search: '', cursor: null }), z.ZodError)
  })
}

test('search and tenant changes cannot reuse pages, and company prefix invalidates all read shapes', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const pageKey = companiesPageOptions('alpha', '').queryKey
  client.setQueryData(pageKey, { pages: [{ items: [row], next_cursor: 'old' }], pageParams: [null] })
  client.setQueryData(companyOptions().queryKey, [{ id: row.id, name: row.name }])
  client.setQueryData(companyStatsOptions('alpha').queryKey, row)
  // When
  await client.invalidateQueries({ queryKey: ['admin-companies'] })
  // Then
  assert.equal(client.getQueryData(companiesPageOptions('alpha', 'new').queryKey), undefined)
  assert.equal(client.getQueryData(companiesPageOptions('beta', '').queryKey), undefined)
  for (const key of [pageKey, companyOptions().queryKey, companyStatsOptions('alpha').queryKey]) {
    assert.equal(client.getQueryState(key)?.isInvalidated, true)
  }
})
