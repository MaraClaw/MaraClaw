import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ApiError } from '../src/lib/http.ts'
import {
  exportResourcePressureCsv,
  getResourcePressureEpisodes,
  getResourcePressureSummary,
  RESOURCE_PRESSURE_EPISODE_PAGE_SIZE,
} from '../src/lib/resource-pressure-api.ts'
import {
  agentId, cpuEpisodeRow, csvBody, episodeCursor, episodeRow, otherTenantId, ownPressureSession,
  pressureToken, quietSummaryRow, summaryRow, tenantId,
} from './resource-pressure-fixtures.ts'

const range = { since: '2026-11-01T00:00:00.000Z', until: '2026-11-08T00:00:00.000Z' }
const everyFilter = { ...range, tier: 'pro', resource: 'memory', tenantId, agentId, kind: 'oom_kill', openOnly: true } as const

function requestOf(call: { arguments: [RequestInfo | URL, RequestInit?] } | undefined) {
  const [url, options] = call?.arguments ?? []
  return { url: new URL(String(url), 'https://example.test'), options }
}

test('summary sends only the summary filters with the admin bearer token and validates every row', async (context) => {
  // Given
  ownPressureSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([summaryRow, quietSummaryRow]))
  // When
  const rows = await getResourcePressureSummary(everyFilter)
  // Then
  const { url, options } = requestOf(fetch.mock.calls[0])
  assert.equal(url.pathname, '/api/admin/resource-pressure/summary')
  assert.deepEqual([...url.searchParams], [
    ['since', range.since], ['until', range.until], ['tier', 'pro'], ['resource', 'memory'], ['tenant_id', tenantId],
  ])
  assert.equal(options?.method, 'GET')
  assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: `Bearer ${pressureToken}` })
  assert.deepEqual(rows, [summaryRow, quietSummaryRow])
  assert.equal(rows[1]?.current_tier, null)
  assert.equal(rows[1]?.suggested_tier, null)
})

test('summary without filters has no query string and drops unknown response keys', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([{ ...summaryRow, internal_note: 'x' }]))
  // When
  const rows = await getResourcePressureSummary({})
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/resource-pressure/summary')
  assert.deepEqual(rows, [summaryRow])
})

test('episodes add the episode filters, the page size and the cursor', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ items: [episodeRow, cpuEpisodeRow], next_cursor: episodeCursor }))
  // When
  const page = await getResourcePressureEpisodes(everyFilter, episodeCursor)
  // Then
  const { url } = requestOf(fetch.mock.calls[0])
  assert.equal(url.pathname, '/api/admin/resource-pressure/episodes')
  assert.deepEqual([...url.searchParams], [
    ['since', range.since], ['until', range.until], ['tier', 'pro'], ['resource', 'memory'], ['tenant_id', tenantId],
    ['agent_id', agentId], ['kind', 'oom_kill'], ['open_only', 'true'], ['limit', String(RESOURCE_PRESSURE_EPISODE_PAGE_SIZE)], ['cursor', episodeCursor],
  ])
  assert.deepEqual(page, { items: [episodeRow, cpuEpisodeRow], next_cursor: episodeCursor })
  assert.equal(page.items[1]?.agent_id, null)
})

test('episodes omit open_only when false and the cursor on the first page', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ items: [], next_cursor: null }))
  // When
  const page = await getResourcePressureEpisodes({ tenantId, openOnly: false }, null)
  // Then
  assert.deepEqual([...requestOf(fetch.mock.calls[0]).url.searchParams], [['tenant_id', tenantId], ['limit', '25']])
  assert.deepEqual(page, { items: [], next_cursor: null })
})

for (const [name, filters] of [
  ['unknown tier', { tier: 'enterprise' }],
  ['unknown resource', { resource: 'gpu' }],
  ['unknown kind', { kind: 'crash' }],
  ['non-UUID tenant', { tenantId: 'acme' }],
  ['non-UUID agent', { agentId: 'sales' }],
  ['date without an offset', { since: '2026-11-01T00:00:00' }],
  ['unexpected key', { limit: 500 }],
] as const) test(`${name} is rejected before transport`, async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When / Then
  await assert.rejects(getResourcePressureSummary(filters as never), z.ZodError)
  await assert.rejects(getResourcePressureEpisodes(filters as never), z.ZodError)
  await assert.rejects(exportResourcePressureCsv(filters as never), z.ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

test('an oversized cursor is rejected before transport', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
  // When / Then
  await assert.rejects(getResourcePressureEpisodes({}, 'x'.repeat(513)), z.ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

for (const [name, body] of [
  ['a missing tenant', [{ ...summaryRow, tenant_id: undefined }]],
  ['a non-numeric ratio', [{ ...summaryRow, max_peak_ratio: '0.97' }]],
  ['an unknown suggested tier', [{ ...summaryRow, suggested_tier: 'enterprise' }]],
  ['missing resource counts', [{ ...summaryRow, episode_counts: { memory: 1 } }]],
  ['an object instead of a list', { items: [] }],
] as const) test(`summary rejects ${name} instead of inventing defaults`, async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json(body))
  // When / Then
  await assert.rejects(getResourcePressureSummary({}), z.ZodError)
})

for (const [name, body] of [
  ['an unknown kind', { items: [{ ...episodeRow, kind: 'crash' }], next_cursor: null }],
  ['a missing agent name', { items: [{ ...episodeRow, agent_name: undefined }], next_cursor: null }],
  ['an empty cursor', { items: [], next_cursor: '' }],
  ['a bare list', [episodeRow]],
] as const) test(`episodes reject ${name}`, async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json(body))
  // When / Then
  await assert.rejects(getResourcePressureEpisodes({}), z.ZodError)
})

test('a platform-admin-only 403 surfaces its detail without a retry', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Insufficient permissions' }, { status: 403 }))
  // When / Then
  await assert.rejects(getResourcePressureSummary({}), (error) => error instanceof ApiError && error.status === 403 && error.message === 'Insufficient permissions')
  assert.equal(fetch.mock.callCount(), 1)
})

test('episode reads forward cancellation to the transport', async (context) => {
  // Given
  const response = Promise.withResolvers<Response>()
  let signal: AbortSignal | null | undefined
  context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, options?: RequestInit) => {
    signal = options?.signal
    return response.promise
  })
  const controller = new AbortController()
  const pending = getResourcePressureEpisodes({}, null, controller.signal)
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json({ items: [], next_cursor: null }))
  // Then
  await rejected
  assert.equal(signal?.aborted, true)
})

test('CSV export sends the current filters with the bearer token and returns the body as text', async (context) => {
  // Given
  ownPressureSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => new Response(csvBody, { headers: { 'Content-Type': 'text/csv; charset=utf-8' } }))
  // When
  const csv = await exportResourcePressureCsv({ ...everyFilter, tenantId: otherTenantId })
  // Then
  const { url, options } = requestOf(fetch.mock.calls[0])
  assert.equal(url.pathname, '/api/admin/resource-pressure/export.csv')
  assert.deepEqual([...url.searchParams], [
    ['since', range.since], ['until', range.until], ['tier', 'pro'], ['resource', 'memory'], ['tenant_id', otherTenantId],
    ['agent_id', agentId], ['kind', 'oom_kill'], ['open_only', 'true'],
  ])
  assert.equal(options?.method, 'GET')
  assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: `Bearer ${pressureToken}` })
  assert.equal(csv, csvBody)
})

test('CSV export of an unfiltered header-only file has no query string', async (context) => {
  // Given
  const header = 'id,tenant_id,tenant_name\r\n'
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => new Response(header))
  // When
  const csv = await exportResourcePressureCsv({})
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/resource-pressure/export.csv')
  assert.equal(csv, header)
})

test('CSV export failure keeps the server detail', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'until must be after since' }, { status: 400 }))
  // When / Then
  await assert.rejects(exportResourcePressureCsv({}), (error) => error instanceof ApiError && error.status === 400 && error.message === 'until must be after since')
})
