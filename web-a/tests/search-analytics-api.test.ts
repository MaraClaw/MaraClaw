import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import {
  getSearchAnalyticsDashboard,
  getSearchAnalyticsExportStatus, getSearchAnalyticsOrgs, getSearchAnalyticsSummary,
  getSearchAnalyticsTimeseries, getSearchAnalyticsTrending,
} from '../src/lib/search-analytics-api.ts'
import { ApiError } from '../src/lib/http.ts'

const metrics = {
  scope: 'system', event_count: 100, error_count: 4, quota_count: 2, unique_orgs: 3,
  unique_agents: 7, unattributed_count: 1, avg_latency_ms: 12.5,
  by_kind: [{ kind: 'search', event_count: 100 }],
}
const envelope = {
  start: '2026-10-01T00:00:00Z', end: '2026-10-08T00:00:00Z', tenant_id: 'selected-company',
  system_summary: metrics, selected_summary: { ...metrics, scope: 'org', event_count: 25 },
  timeseries: [{ date: '2026-10-02', event_count: 25, error_count: 1, quota_count: 0 }],
  orgs: [{ tenant_id: 'another-company', name: 'Global leader', event_count: 60, quota_count: 2 }],
  trending: [{ query_hash: 'hash', query_normalized: 'normalized query', kind: 'search', hits: 4, distinct_agents: 2, distinct_orgs: 1 }],
}

test('dashboard validates mixed scopes and forwards every window and tenant filter', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(envelope))
  // When
  const result = await getSearchAnalyticsDashboard({ start: envelope.start, end: envelope.end, tenantId: 'org/a?&=' })
  // Then: the public wire contract preserves independent global/scoped aggregates.
  assert.deepEqual(result, envelope)
  const url = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
  assert.equal(url.pathname, '/api/admin/search-analytics/dashboard')
  assert.deepEqual([...url.searchParams], [['start', envelope.start], ['end', envelope.end], ['tenant_id', 'org/a?&=']])
})

test('dashboard propagates query cancellation through transport and rejects its late body', async (context) => {
  // Given
  const response = Promise.withResolvers<Response>()
  let signal: AbortSignal | null | undefined
  context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, options?: RequestInit) => {
    signal = options?.signal
    return response.promise
  })
  const controller = new AbortController()
  const pending = getSearchAnalyticsDashboard({}, controller.signal)
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json(envelope))
  // Then
  await rejected
  assert.equal(signal?.aborted, true)
})

for (const response of [
  { ...envelope, system_summary: { ...metrics, event_count: null } },
  { ...envelope, selected_summary: { ...metrics, scope: 'all' } },
  { ...envelope, timeseries: [{ date: '2026-10-02', event_count: '25', error_count: 0, quota_count: 0 }] },
  { ...envelope, orgs: {} },
  { ...envelope, trending: [{ query_hash: 'hash', hits: 4 }] },
  { system_summary: metrics },
]) test('dashboard rejects malformed aggregates instead of inventing defaults', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json(response))
  // When / Then
  await assert.rejects(getSearchAnalyticsDashboard({}), z.ZodError)
})

test('dashboard preserves server failure without legacy endpoint fallback', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Aggregation unavailable' }, { status: 503 }))
  // When / Then
  await assert.rejects(getSearchAnalyticsDashboard({}), (error) => error instanceof ApiError && error.status === 503 && error.message === 'Aggregation unavailable')
  assert.equal(fetch.mock.callCount(), 1)
})

test('summary adapts omitted generated defaults for the analytics UI', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ scope: 'system', by_kind: [{}] }))
  // When
  const summary = await getSearchAnalyticsSummary({})
  // Then
  assert.deepEqual(summary, {
    scope: 'system', by_kind: [{ kind: '', event_count: 0 }], event_count: 0, error_count: 0,
    quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0,
  })
})

test('summary preserves real metrics and encodes every filter as one query value', async (context) => {
  // Given
  const response = { scope: 'org', event_count: 17, avg_latency_ms: 2.5 }
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(response))
  const controller = new AbortController()
  // When
  const summary = await getSearchAnalyticsSummary({ start: '2026-10-01', end: '2026-10-25', tenantId: 'org/a?&=' }, controller.signal)
  // Then
  assert.equal(summary.event_count, 17)
  assert.equal(summary.avg_latency_ms, 2.5)
  const url = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
  assert.deepEqual([...url.searchParams], [['start', '2026-10-01'], ['end', '2026-10-25'], ['tenant_id', 'org/a?&=']])
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal?.aborted, false)
})

for (const response of [{ scope: 'all' }, { scope: 'system', event_count: null }, { scope: 'org', error_count: 1.5 }, { scope: 'system', by_kind: [{ event_count: '2' }] }]) {
  test('summary rejects invalid generated metric fields instead of defaulting them', async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When / Then
    await assert.rejects(getSearchAnalyticsSummary({}), z.ZodError)
  })
}

for (const [read, expected] of [
  [getSearchAnalyticsTimeseries, { date: '', event_count: 0, error_count: 0, quota_count: 0 }],
  [getSearchAnalyticsOrgs, { tenant_id: null, name: '', event_count: 0, quota_count: 0 }],
  [getSearchAnalyticsTrending, { query_hash: '', query_normalized: '', kind: '', hits: 0, distinct_agents: 0, distinct_orgs: 0 }],
] as const) {
  test(`${read.name} adapts optional generated values for the analytics UI`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json([{}]))
    // When
    const result = await read({})
    // Then
    assert.deepEqual(result, [expected])
  })
  test(`${read.name} rejects a non-array envelope`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [] }))
    // When / Then
    await assert.rejects(read({}), z.ZodError)
  })
}

test('export status retains nullable configuration and adapts omitted counters', async (context) => {
  // Given
  const response = { capture_enabled: true, export_enabled: false, include_raw: false, bucket: null, prefix: 'search/' }
  context.mock.method(globalThis, 'fetch', async () => Response.json(response))
  // When
  const result = await getSearchAnalyticsExportStatus()
  // Then
  assert.deepEqual(result, { ...response, pending: 0, exporting: 0, exported: 0, skipped: 0, last_exported_at: null })
})
