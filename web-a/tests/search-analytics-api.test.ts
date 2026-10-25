import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import {
  getSearchAnalyticsExportStatus, getSearchAnalyticsOrgs, getSearchAnalyticsSummary,
  getSearchAnalyticsTimeseries, getSearchAnalyticsTrending,
} from '../src/lib/search-analytics-api.ts'

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
