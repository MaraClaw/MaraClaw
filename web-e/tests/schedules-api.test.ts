import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createSchedule, listSchedules, updateSchedule } from '../src/lib/workspace-api.ts'

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
