import assert from 'node:assert/strict'
import test from 'node:test'
import { InfiniteQueryObserver, QueryClient, skipToken } from '@tanstack/react-query'
import { z } from 'zod'

import {
  companiesPageOptions,
  companyOptionByIdOptions,
  companyOptionsBrowseOptions,
  companyOptionsSearchOptions,
  companyStatsOptions,
  isCompanyId,
} from '../src/hooks/company-options.ts'
import {
  createCompany, getCompanyStats, listCompanies, listCompaniesPage, listCompanyOptions, type CompanyOption,
} from '../src/lib/companies-api.ts'
import { ApiError } from '../src/lib/http.ts'

const row = {
  id: 'alpha', name: 'Zebra Company', slug: 'zebra', is_active: true,
  sso_enabled: false, sso_domain: null, is_system: false, is_default_end_user_org: false,
  can_disable: true, created_at: null, user_count: 7, active_user_count: 5,
  inactive_user_count: 2, agent_count: 3, org_admin_email: null, phone: '+84912345678', tier: 'pro' as const,
}

const companyId = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const option = (n: number, name = `Company ${n}`) => ({ id: companyId(n), name, slug: `company-${n}`, is_active: true, can_disable: true })

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

test('picker options send search or an opaque cursor, parse stats-free pages and forward cancellation', async (t) => {
  // Given
  const page = { items: [option(1, 'Zebra'), option(2, 'Alpha')], next_cursor: 'opaque/next +?=' }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json(page))
  const signal = new AbortController().signal
  // When
  const browsed = await listCompanyOptions({ q: '', cursor: 'opaque/cursor +?=' }, signal)
  const searched = await listCompanyOptions({ q: ' %_Zeb+? ', cursor: null })
  // Then
  const [first, second] = fetch.mock.calls.map(call => new URL(String(call.arguments[0]), 'https://example.test'))
  assert.equal(first?.pathname, '/api/admin/companies/options')
  assert.deepEqual(Object.fromEntries(first?.searchParams ?? []), { limit: '50', cursor: 'opaque/cursor +?=' })
  assert.deepEqual(Object.fromEntries(second?.searchParams ?? []), { limit: '50', q: '%_Zeb+?' })
  assert.strictEqual(fetch.mock.calls[0]?.arguments[1]?.signal, signal)
  assert.deepEqual([browsed, searched], [page, page])
})

for (const body of [[option(1)], { items: [{ id: 'alpha', name: 'Alpha' }], next_cursor: null }, { items: [], next_cursor: 7 }]) {
  test(`picker options reject the legacy array and malformed pages: ${JSON.stringify(body)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(listCompanyOptions({ q: '', cursor: null }), z.ZodError)
  })
}

test('picker browse appends pages on demand while search is one ranked page with no load-more', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const requests: Record<string, string>[] = []
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => {
    const request = new URL(String(url), 'https://example.test')
    requests.push(Object.fromEntries(request.searchParams))
    if (request.searchParams.has('q')) return Response.json({ items: [option(9, 'Old Zebra')], next_cursor: null })
    const cursor = request.searchParams.get('cursor')
    return Response.json({ items: [option(cursor ? 2 : 1)], next_cursor: cursor ? null : 'next/+=' })
  })
  const browse = new InfiniteQueryObserver(client, companyOptionsBrowseOptions())
  t.after(browse.subscribe(() => undefined))
  await browse.refetch()
  // When
  const more = await browse.fetchNextPage()
  const found = await client.fetchQuery(companyOptionsSearchOptions('  Zebra '))
  // Then
  assert.deepEqual(more.data?.pages.flatMap(page => page.items.map(item => item.id)), [companyId(1), companyId(2)])
  assert.equal(more.hasNextPage, false)
  assert.deepEqual(found, { items: [option(9, 'Old Zebra')], next_cursor: null })
  assert.deepEqual(requests, [{ limit: '50' }, { limit: '50', cursor: 'next/+=' }, { limit: '50', q: 'Zebra' }])
  assert.deepEqual(companyOptionsBrowseOptions().queryKey, ['admin-companies', 'options', 'browse'])
  assert.deepEqual(companyOptionsSearchOptions(' Zebra ').queryKey, ['admin-companies', 'options', 'search', 'Zebra'])
  assert.equal(companyOptionsSearchOptions('   ').queryFn, skipToken)
})

test('picker resolves a selection by id: 404 is unknown, errors stay errors, a malformed id is never sent', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => {
    const path = String(url)
    if (path.endsWith(companyId(1))) return Response.json({ id: companyId(1), name: 'Old Co', slug: 'old', is_active: true, tier: 'pro' })
    return path.endsWith(companyId(2))
      ? Response.json({ detail: 'Tenant not found' }, { status: 404 })
      : Response.json({ detail: 'Unavailable' }, { status: 503 })
  })
  // When
  const resolved = await client.fetchQuery(companyOptionByIdOptions(companyId(1)))
  const missing = await client.fetchQuery(companyOptionByIdOptions(companyId(2)))
  // Then
  assert.deepEqual(resolved, { id: companyId(1), name: 'Old Co' })
  assert.equal(missing, null)
  await assert.rejects(client.fetchQuery(companyOptionByIdOptions(companyId(3))), error => error instanceof ApiError && error.status === 503)
  assert.deepEqual(fetch.mock.calls.map(call => call.arguments[0]), [1, 2, 3].map(n => `/api/tenants/${companyId(n)}`))
  assert.deepEqual(companyOptionByIdOptions(companyId(1)).queryKey, ['admin-companies', 'options', 'by-id', companyId(1)])
  for (const value of ['', 'alpha', `${companyId(1)}x`]) {
    assert.equal(isCompanyId(value), false)
    assert.equal(companyOptionByIdOptions(value).queryFn, skipToken)
  }
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

test('company creation sends the chosen plan and phone number', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({ company: { ...row, tier: 'max' }, org_admin_email: 'admin@example.test', must_change_password: true }))
  // When
  await createCompany({
    name: 'Zebra Company', phone: '+84912345678', tier: 'max', admin_email: 'admin@example.test', admin_password: 'secret-1',
  })
  // Then
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/admin/companies')
  assert.equal(init?.method, 'POST')
  assert.deepEqual(JSON.parse(String(init?.body)), {
    name: 'Zebra Company', phone: '+84912345678', tier: 'max', admin_email: 'admin@example.test', admin_password: 'secret-1',
  })
})

test('company rows carry each plan', async (t) => {
  // Given
  const rows = ['pro', 'max', 'ultra'].map(tier => ({ ...row, id: tier, tier }))
  t.mock.method(globalThis, 'fetch', async () => Response.json({ items: rows, next_cursor: null }))
  // When
  const page = await listCompaniesPage({ search: '', cursor: null })
  // Then
  assert.deepEqual(page.items.map(item => item.tier), ['pro', 'max', 'ultra'])
})

test('company stats reject a missing plan', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...row, tier: null }))
  // When / Then
  await assert.rejects(getCompanyStats('alpha'), z.ZodError)
})

const rowWithoutTier = Object.fromEntries(Object.entries(row).filter(([key]) => key !== 'tier'))
for (const body of [
  [], { items: [], next_cursor: 42 }, { items: [{ ...row, user_count: '7' }], next_cursor: null },
  { items: [{ ...row, tier: 'gold' }], next_cursor: null }, { items: [rowWithoutTier], next_cursor: null },
]) {
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
  client.setQueryData(companyOptionsBrowseOptions().queryKey, { pages: [{ items: [option(1)], next_cursor: 'old' }], pageParams: [null] })
  client.setQueryData(companyOptionsSearchOptions('zeb').queryKey, { items: [option(1)], next_cursor: null })
  const resolved: CompanyOption | null = { id: companyId(1), name: 'Company 1' }
  client.setQueryData(companyOptionByIdOptions(companyId(1)).queryKey, resolved)
  client.setQueryData(companyStatsOptions('alpha').queryKey, row)
  // When
  await client.invalidateQueries({ queryKey: ['admin-companies'] })
  // Then
  assert.equal(client.getQueryData(companiesPageOptions('alpha', 'new').queryKey), undefined)
  assert.equal(client.getQueryData(companiesPageOptions('beta', '').queryKey), undefined)
  const pickerKeys = [
    companyOptionsBrowseOptions().queryKey,
    companyOptionsSearchOptions('zeb').queryKey,
    companyOptionByIdOptions(companyId(1)).queryKey,
  ]
  for (const key of [pageKey, ...pickerKeys, companyStatsOptions('alpha').queryKey]) {
    assert.equal(client.getQueryState(key)?.isInvalidated, true)
  }
})
