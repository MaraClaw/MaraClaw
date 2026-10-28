import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createSchedule, listSchedules, updateSchedule } from '../src/lib/workspace-api.ts'
import { listScheduleHistory, listScheduleHistoryPage } from '../src/lib/schedules-api.ts'

const saved = {
  id: 'schedule', name: 'Morning report', instruction: 'Summarize updates',
  cron_expr: '0 9 * * *', is_enabled: true, timezone: 'Asia/Shanghai', notify_on_failure: false,
  next_run_at: '2026-01-15T01:00:00Z', last_run_at: null,
}

test('schedule creation sends UTC when timezone is omitted', async (context) => {
  const bodies: unknown[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    bodies.push(JSON.parse(String(init.body)))
    return Response.json({ ...saved, timezone: 'UTC' })
  })
  await createSchedule('agent', { name: saved.name, instruction: saved.instruction, cron_expr: saved.cron_expr })
  assert.deepEqual(bodies, [{ name: saved.name, instruction: saved.instruction, cron_expr: saved.cron_expr, timezone: 'UTC', notify_on_failure: false }])
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

for (const notify_on_failure of [true, false]) {
  test(`creation and read preserve an explicit ${notify_on_failure} failure alert preference`, async (context) => {
    const schedule = { ...saved, notify_on_failure }
    context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
      if (init.method === 'POST') {
        assert.equal(JSON.parse(String(init.body)).notify_on_failure, notify_on_failure)
        return Response.json(schedule)
      }
      return Response.json([schedule])
    })

    const created = await createSchedule('agent', schedule)
    const reloaded = await listSchedules('agent')

    assert.deepEqual(created, schedule)
    assert.deepEqual(reloaded, [schedule])
  })

  test(`failure alert patch sends explicit ${notify_on_failure} without unrelated fields`, async (context) => {
    const patch = { notify_on_failure }
    context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
      assert.deepEqual(JSON.parse(String(init.body)), patch)
      return Response.json({ ...saved, ...patch })
    })

    const patched = await updateSchedule('agent', saved.id, patch)

    assert.deepEqual(patched, { ...saved, ...patch })
  })
}

test('patch omission preserves an enabled failure alert preference', async (context) => {
  const schedule = { ...saved, notify_on_failure: true }
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    assert.deepEqual(JSON.parse(String(init.body)), { name: 'Renamed' })
    return Response.json({ ...schedule, name: 'Renamed' })
  })

  const patched = await updateSchedule('agent', saved.id, { name: 'Renamed' })

  assert.deepEqual(patched, { ...schedule, name: 'Renamed' })
})

for (const notify_on_failure of [null, 'false', 0]) {
  test(`read rejects a nonboolean failure alert preference: ${JSON.stringify(notify_on_failure)}`, async (context) => {
    context.mock.method(globalThis, 'fetch', async () => Response.json([{ ...saved, notify_on_failure }]))

    await assert.rejects(() => listSchedules('agent'))
  })
}

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

for (const ends_at of [null, '2030-01-16T01:30:00Z', '2030-01-16T09:30:45.123456+08:00']) {
  test(`cutoff ${String(ends_at)} survives creation and reload`, async (context) => {
    // Given
    const schedule = { ...saved, ends_at }
    context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
      if (init.method === 'POST') {
        assert.equal(JSON.parse(String(init.body)).ends_at, ends_at)
        return Response.json(schedule)
      }
      return Response.json([schedule])
    })
    // When
    const created = await createSchedule('agent', schedule)
    const reloaded = await listSchedules('agent')
    // Then
    assert.deepEqual(created, schedule)
    assert.deepEqual(reloaded, [schedule])
  })

  test(`cutoff-only patch sends explicit ${String(ends_at)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
      assert.deepEqual(JSON.parse(String(init.body)), { ends_at })
      return Response.json({ ...saved, ends_at })
    })
    // When / Then
    assert.equal((await updateSchedule('agent', saved.id, { ends_at })).ends_at, ends_at)
  })
}

test('unrelated patch omits and preserves a saved cutoff', async (context) => {
  // Given
  const schedule = { ...saved, ends_at: '2030-01-16T09:30:45.123456+08:00' }
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    assert.deepEqual(JSON.parse(String(init.body)), { timezone: 'UTC' })
    return Response.json({ ...schedule, timezone: 'UTC' })
  })
  // When / Then
  assert.equal((await updateSchedule('agent', saved.id, { timezone: 'UTC' })).ends_at, schedule.ends_at)
})

for (const ends_at of ['2030-01-16T01:30:00', '2030-02-30T01:30:00Z', '', 1894757400, false]) {
  test(`read rejects malformed cutoff ${JSON.stringify(ends_at)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json([{ ...saved, ends_at }]))
    // When / Then
    await assert.rejects(() => listSchedules('agent'))
  })
}

for (const ends_at of ['2030-01-16T01:30:00', '2030-02-30T01:30:00Z', '', 1894757400]) {
  test(`write rejects malformed cutoff ${JSON.stringify(ends_at)} before HTTP`, async (context) => {
    // Given
    const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json(saved))
    // When / Then
    await assert.rejects(() => Reflect.apply(createSchedule, undefined, ['agent', { ...saved, ends_at }]))
    await assert.rejects(() => Reflect.apply(updateSchedule, undefined, ['agent', saved.id, { ends_at }]))
    assert.equal(fetch.mock.callCount(), 0)
  })
}
