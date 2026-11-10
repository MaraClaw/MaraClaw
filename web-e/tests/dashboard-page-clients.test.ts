import assert from 'node:assert/strict'
import { test } from 'node:test'
import { z } from 'zod'

import { listObjectivePage, listObjectives } from '../src/lib/okr-api.ts'
import { addRelationship, listRelationshipPage } from '../src/lib/relationships-api.ts'

const period = { periodStart: '2026-10-01', periodEnd: '2026-12-31' }
const cursor = 'opaque/+?='

test('objective pages encode period and opaque cursor, forward cancellation and keep every key result', async (t) => {
  const controller = new AbortController()
  const objective = { id: 'objective', title: 'Complete objective', owner_type: 'company', status: 'active',
    period_start: period.periodStart, period_end: period.periodEnd, created_at: '2026-10-01T00:00:00Z',
    key_results: Array.from({ length: 60 }, (_, index) => ({ id: `kr-${index}`, objective_id: 'objective', title: `Result ${index}`,
      target_value: 100, current_value: index, status: 'on_track', created_at: '2026-10-01T00:00:00Z', last_updated_at: '2026-10-01T00:00:00Z' })) }
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture.test')
    assert.equal(url.pathname, '/api/okr/objectives/page')
    assert.deepEqual(Object.fromEntries(url.searchParams), { period_start: period.periodStart, period_end: period.periodEnd, limit: '50', cursor })
    assert.equal(init?.signal, controller.signal)
    return Response.json({ items: [objective], next_cursor: null })
  })
  const result = await listObjectivePage({ ...period, cursor, signal: controller.signal })
  assert.equal(result.items[0]?.key_results?.length, 60)
  assert.equal(result.next_cursor, null)
})

test('legacy objective client still reads the unpaged array contract', async (t) => {
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    assert.equal(new URL(String(input), 'http://fixture.test').pathname, '/api/okr/objectives')
    return Response.json([])
  })
  assert.deepEqual(await listObjectives(), [])
})

test('relationship pages forward cursors and signals; adding sends only the selected relationship by POST', async (t) => {
  const controller = new AbortController()
  const body = { member_id: 'selected-member', relation: 'mentor', description: '' }
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture.test')
    calls.push(init?.method ?? 'GET')
    if (init?.method === 'POST') {
      assert.equal(url.pathname, '/api/agents/agent/relationships/')
      assert.deepEqual(JSON.parse(String(init.body)), body)
      return Response.json({ status: 'ok' })
    }
    assert.equal(url.pathname, '/api/agents/agent/relationships/page')
    assert.deepEqual(Object.fromEntries(url.searchParams), { limit: '50', cursor })
    assert.equal(init?.signal, controller.signal)
    return Response.json({ items: [], next_cursor: null, has_more: false })
  })
  assert.deepEqual(await listRelationshipPage('agent', { cursor, signal: controller.signal }), { items: [], next_cursor: null, has_more: false })
  await addRelationship('agent', body)
  assert.deepEqual(calls, ['GET', 'POST'])
})

for (const malformed of [[], {}, { items: [], next_cursor: 4 }, { items: [null], next_cursor: null }]) {
  test(`generated page schemas reject malformed dashboard responses ${JSON.stringify(malformed)}`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    await assert.rejects(listObjectivePage({ ...period, cursor: null }), z.ZodError)
    await assert.rejects(listRelationshipPage('agent', { cursor: null }), z.ZodError)
  })
}

test('generated relationship acknowledgement rejects an invalid mutation response', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'failed' }))
  await assert.rejects(addRelationship('agent', { member_id: 'member', relation: 'mentor', description: '' }), z.ZodError)
})

for (const domain of ['objectives', 'relationships'] as const) {
  test(`${domain} page rejects a late response after its request is cancelled`, async (t) => {
    const controller = new AbortController()
    const started = Promise.withResolvers<void>()
    const response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', async (_input: RequestInfo | URL, init?: RequestInit) => {
      assert.equal(init?.signal, controller.signal)
      started.resolve()
      return response.promise
    })
    const pending = domain === 'objectives'
      ? listObjectivePage({ ...period, cursor, signal: controller.signal })
      : listRelationshipPage('agent', { cursor, signal: controller.signal })
    const rejected = assert.rejects(pending, { name: 'AbortError' })
    await started.promise
    controller.abort()
    response.resolve(Response.json({ items: [], next_cursor: null, has_more: false }))
    await rejected
  })
}
