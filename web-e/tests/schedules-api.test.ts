import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createSchedule, listSchedules, updateSchedule } from '../src/lib/workspace-api.ts'
import { listScheduleHistory, listScheduleHistoryPage } from '../src/lib/schedules-api.ts'

const saved = {
  id: 'schedule', name: 'Morning report', instruction: 'Summarize updates',
  cron_expr: '0 9 * * *', is_enabled: true, timezone: 'Asia/Shanghai',
  next_run_at: '2026-01-15T01:00:00Z', last_run_at: null,
}

test('schedule creation sends UTC when timezone is omitted', async (context) => {
  const bodies: unknown[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    bodies.push(JSON.parse(String(init.body)))
    return Response.json({ ...saved, timezone: 'UTC' })
  })
  await createSchedule('agent', { name: saved.name, instruction: saved.instruction, cron_expr: saved.cron_expr })
  assert.deepEqual(bodies, [{ name: saved.name, instruction: saved.instruction, cron_expr: saved.cron_expr, timezone: 'UTC' }])
})

test('explicit timezone survives schedule creation and reload', async (context) => {
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    if (init.method === 'POST') {
      assert.equal(JSON.parse(String(init.body)).timezone, saved.timezone)
      return Response.json(saved)
    }
    return Response.json([saved])
  })
  const created = await createSchedule('agent', saved)
  const reloaded = await listSchedules('agent')
  assert.deepEqual(created, saved)
  assert.deepEqual(reloaded, [saved])
})

test('patch omission leaves timezone out of the request', async (context) => {
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/agents/agent/schedules/schedule')
    assert.equal(init.method, 'PATCH')
    assert.deepEqual(JSON.parse(String(init.body)), { name: 'Renamed report' })
    return Response.json({ ...saved, name: 'Renamed report' })
  })
  const patched = await updateSchedule('agent', saved.id, { name: 'Renamed report' })
  assert.deepEqual(patched, { ...saved, name: 'Renamed report' })
})

test('timezone-only patch returns the server-recomputed next run', async (context) => {
  const patch = { timezone: 'America/New_York' }
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    assert.deepEqual(JSON.parse(String(init.body)), patch)
    return Response.json({ ...saved, ...patch, next_run_at: '2026-01-15T14:00:00Z' })
  })
  const patched = await updateSchedule('agent', saved.id, patch)
  assert.deepEqual(patched, { ...saved, ...patch, next_run_at: '2026-01-15T14:00:00Z' })
})

test('paged history preserves terminal outcomes, output, and the opaque cursor', async (context) => {
  const items = ['success', 'failure', 'cancelled', 'unknown'].map((status) => ({
    id: status, created_at: '2026-10-20T00:00:00Z', status,
    summary: 'Run summary', instruction: 'Run instruction', reply: 'Run reply',
  }))
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    assert.equal(path, '/api/agents/agent/schedules/schedule/history/page?limit=20')
    return Response.json({ items, next_cursor: 'opaque/+?=& cursor' })
  })

  const page = await listScheduleHistoryPage('agent', 'schedule')

  assert.deepEqual(page, { items, next_cursor: 'opaque/+?=& cursor' })
})

test('older history passes an encoded cursor unchanged and supports cancellation', async (context) => {
  const cursor = 'opaque/+?=& cursor'
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    const url = new URL(path, 'https://example.test')
    assert.equal(url.pathname, '/api/agents/agent/schedules/schedule/history/page')
    assert.equal(url.searchParams.get('cursor'), cursor)
    assert.equal(url.searchParams.get('limit'), '20')
    assert.equal(init.signal, controller.signal)
    return Response.json({ items: [], next_cursor: null })
  })

  const page = await listScheduleHistoryPage('agent', 'schedule', { cursor, signal: controller.signal })

  assert.deepEqual(page, { items: [], next_cursor: null })
})

for (const outcome of ['all', 'success', 'failure', 'cancelled', 'unknown'] as const) {
  test(`history forwards ${outcome} alongside its scoped cursor without changing the page contract`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const query = new URL(path, 'https://example.test').searchParams
      assert.equal(query.get('outcome'), outcome)
      assert.equal(query.get('cursor'), 'filter/+ cursor')
      assert.equal(query.get('limit'), '20')
      assert.equal(init.signal, signal)
      return Response.json({ items: [], next_cursor: null })
    })
    // When
    const page = await listScheduleHistoryPage('agent', 'schedule', { outcome, cursor: 'filter/+ cursor', signal })
    // Then
    assert.deepEqual(page, { items: [], next_cursor: null })
  })
}

test('missing status remains unknown even when paged history has a successful-looking reply', async (context) => {
  const legacy = { id: 'legacy', summary: 'Completed successfully', reply: 'Done' }
  context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [legacy], next_cursor: null }))

  const page = await listScheduleHistoryPage('agent', 'schedule')

  assert.deepEqual(page.items, [{ ...legacy, status: 'unknown' }])
})

test('legacy array history preserves explicit status and treats absent status as unknown', async (context) => {
  const items = [{ id: 'failed', status: 'failure', instruction: 'Report', reply: 'Partial output' }, { id: 'old', reply: 'Done' }]
  context.mock.method(globalThis, 'fetch', async () => Response.json(items))

  const history = await listScheduleHistory('agent', 'schedule')

  assert.deepEqual(history, [items[0], { ...items[1], status: 'unknown' }])
})

for (const response of [
  { items: [{ id: 'invalid', status: 'running' }], next_cursor: null },
  { items: [], next_cursor: 20 },
  { items: [] },
]) {
  test(`malformed paged history is rejected: ${JSON.stringify(response)}`, async (context) => {
    context.mock.method(globalThis, 'fetch', async () => Response.json(response))

    await assert.rejects(() => listScheduleHistoryPage('agent', 'schedule'))
  })
}
