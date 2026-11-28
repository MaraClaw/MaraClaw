import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { companyActivationOptions, companyRuntimeOptions, companyTierChangeOptions, companyTierPreviewOptions } from '../src/hooks/company-options.ts'
import { changeCompanyTier, getCompanyRuntime, previewCompanyTier, toggleCompany } from '../src/lib/companies-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { companyId, runtimePage, tierPreview } from './company-operations-fixtures.ts'
import { assertForwardsCancellation } from './helpers.ts'

test('desired activation sends an encoded id and explicit state, and validates restore counts', async t => {
  const result = { ok: true, is_active: true, restored_members: 3, inactive_members: 2 }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json(result))
  assert.deepEqual(await toggleCompany({ companyId: 'company/+', isActive: true }), result)
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/admin/companies/company%2F%2B/toggle')
  assert.equal(init?.method, 'PUT')
  assert.deepEqual(JSON.parse(String(init?.body)), { is_active: true })
  fetch.mock.mockImplementation(async () => Response.json({ ok: true, is_active: true }))
  await assert.rejects(toggleCompany({ companyId, isActive: true }), z.ZodError)
})

test('tier preview and apply use independent validated endpoints and preserve expected tier', async t => {
  const caller = new AbortController()
  const { signal } = caller
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL, _init?: RequestInit) => Response.json(String(url).endsWith('/preview')
    ? tierPreview : { tenant_id: companyId, previous_tier: 'max', tier: 'pro', changed: true, closed_pressure_episodes: 2 }))
  const preview = await previewCompanyTier('company/+', 'pro', signal)
  assert.equal(preview.accounts.over_limit, 4)
  assert.equal(preview.running_guests.length, 1)
  assertForwardsCancellation(fetch.mock.calls[0]?.arguments[1]?.signal, caller)
  await changeCompanyTier('company/+', { target_tier: 'pro', expected_tier: 'max' })
  assert.deepEqual(fetch.mock.calls.map(call => [call.arguments[0], call.arguments[1]?.method, JSON.parse(String(call.arguments[1]?.body))]), [
    ['/api/admin/companies/company%2F%2B/tier/preview', 'POST', { target_tier: 'pro' }],
    ['/api/admin/companies/company%2F%2B/tier', 'PATCH', { target_tier: 'pro', expected_tier: 'max' }],
  ])
  fetch.mock.mockImplementation(async () => Response.json({ ...tierPreview, accounts: { used: 'seven' } }))
  await assert.rejects(previewCompanyTier(companyId, 'pro'), z.ZodError)
})

test('runtime pages bind company and opaque cursor, forward cancellation and preserve zero diagnostics', async t => {
  const caller = new AbortController()
  const { signal } = caller
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json(runtimePage))
  const result = await getCompanyRuntime('company/+', 'opaque/+ ?=', signal)
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  const request = new URL(String(url), 'https://example.test')
  assert.equal(request.pathname, '/api/admin/companies/company%2F%2B/runtime')
  assert.deepEqual(Object.fromEntries(request.searchParams), { limit: '50', cursor: 'opaque/+ ?=' })
  assertForwardsCancellation(init?.signal, caller)
  assert.equal(result.items[0]?.observation.exit_code, 0)
  assert.equal(result.items[0]?.observation.running, false)
  assert.equal(result.items[0]?.observation.oom_killed_since_last_start, false)
  assert.equal(result.items[0]?.workspace?.used_bytes, 0)
  fetch.mock.mockImplementation(async () => Response.json({ ...runtimePage, items: [{ ...runtimePage.items[0], observation: { state: 'stopped' } }] }))
  await assert.rejects(getCompanyRuntime(companyId, null), z.ZodError)
})

test('inventory caches isolate companies and pages; preview is idle until a target is reviewed', async t => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(runtimePage))
  const first = companyRuntimeOptions(companyId, null)
  const second = companyRuntimeOptions(companyId, 'next')
  const foreign = companyRuntimeOptions('another-company', null)
  client.setQueryData(first.queryKey, runtimePage)
  assert.equal(client.getQueryData(second.queryKey), undefined)
  assert.equal(client.getQueryData(foreign.queryKey), undefined)
  assert.notDeepEqual(companyTierPreviewOptions(companyId, 'pro').queryKey, companyTierPreviewOptions(companyId, 'ultra').queryKey)
  assert.equal(typeof companyTierPreviewOptions(companyId, null).queryFn, 'symbol')
  await client.fetchQuery(second)
  assert.equal(fetch.mock.calls.length, 1)
})

for (const operation of ['activation', 'tier'] as const) {
  test(`${operation} success invalidates the shared company family and its affected adjacent surfaces`, async t => {
    const client = new QueryClient()
    t.after(() => client.clear())
    for (const family of ['admin-companies', 'admin-users', 'admin-user', 'admin-resource-pressure', 'admin-capacity', 'unrelated']) client.setQueryData([family, 'detail'], {})
    t.mock.method(globalThis, 'fetch', async () => Response.json(operation === 'activation'
      ? { ok: true, is_active: true, restored_members: 3, inactive_members: 2 }
      : { tenant_id: companyId, previous_tier: 'max', tier: 'pro', changed: true, closed_pressure_episodes: 2 }))
    if (operation === 'activation') await new MutationObserver(client, companyActivationOptions(client)).mutate({ companyId, isActive: true })
    else await new MutationObserver(client, companyTierChangeOptions(client, companyId)).mutate({ target_tier: 'pro', expected_tier: 'max' })
    const affected = operation === 'activation' ? ['admin-companies', 'admin-users', 'admin-user'] : ['admin-companies', 'admin-resource-pressure', 'admin-capacity']
    for (const family of affected) assert.equal(client.getQueryState([family, 'detail'])?.isInvalidated, true)
    assert.equal(client.getQueryState(['unrelated', 'detail'])?.isInvalidated, false)
  })
}

test('a tier conflict refreshes company stats and preview without retrying or invalidating pressure', async t => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const stats = ['admin-companies', 'stats', companyId]
  const preview = companyTierPreviewOptions(companyId, 'pro').queryKey
  for (const key of [stats, preview, ['admin-resource-pressure']]) client.setQueryData(key, {})
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code: 'company_tier_changed', message: 'Tier changed' } }, { status: 409 }))
  await assert.rejects(new MutationObserver(client, companyTierChangeOptions(client, companyId)).mutate({ target_tier: 'pro', expected_tier: 'max' }), error => error instanceof ApiError && error.status === 409)
  assert.equal(fetch.mock.calls.length, 1)
  assert.equal(client.getQueryState(stats)?.isInvalidated, true)
  assert.equal(client.getQueryState(preview)?.isInvalidated, true)
  assert.equal(client.getQueryState(['admin-resource-pressure'])?.isInvalidated, false)
})
