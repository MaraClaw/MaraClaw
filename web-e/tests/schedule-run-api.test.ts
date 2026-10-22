import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { cancelScheduleRun, getScheduleRun, runSchedule, type ScheduleRunReceipt } from '../src/lib/schedules-api.ts'
import { ApiError } from '../src/lib/http.ts'

const accepted = { status: 'triggered', schedule_id: 'schedule', run_id: 'durable-run' } as const
const target = { agentId: 'agent', scheduleId: 'schedule', runId: 'durable-run' }
const receipt: ScheduleRunReceipt = {
  run_id: 'durable-run', schedule_id: 'schedule', agent_id: 'agent', requester_id: 'requester',
  status: 'pending', created_at: '2026-10-22T06:00:00+00:00', started_at: null, finished_at: null, can_cancel: true,
}

for (const key of [undefined, 'intent-opaque-1']) {
  test(`manual Run ${key === undefined ? 'without a key' : 'with a key'} parses durable acceptance, not completion`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      assert.equal(path, '/api/agents/agent/schedules/schedule/run')
      assert.equal(init.method, 'POST')
      assert.equal(init.body, undefined)
      assert.equal(new Headers(init.headers).get('Idempotency-Key'), key ?? null)
      return Response.json(accepted)
    })
    // When
    const result = await runSchedule('agent', 'schedule', key)
    // Then
    assert.deepEqual(result, accepted)
  })
}

for (const operation of [getScheduleRun, cancelScheduleRun]) {
  const cancelling = operation === cancelScheduleRun
  test(`${operation.name} uses the scoped receipt endpoint, signal and no request body or intent header`, async (context) => {
    // Given
    const controller = new AbortController()
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      assert.equal(path, `/api/agents/agent/schedules/schedule/runs/durable-run${cancelling ? '/cancel' : ''}`)
      assert.equal(init.method, cancelling ? 'POST' : 'GET')
      assert.equal(init.body, undefined)
      assert.equal(init.signal, controller.signal)
      assert.equal(new Headers(init.headers).get('Idempotency-Key'), null)
      return Response.json(receipt)
    })
    // When
    const result = await operation(target, { signal: controller.signal })
    // Then
    assert.deepEqual(result, receipt)
  })

  for (const status of ['pending', 'running', 'success', 'failure', 'cancelled', 'unknown'] as const) {
    test(`${operation.name} parses ${status} as a typed receipt`, async (context) => {
      // Given
      const body = { ...receipt, status, can_cancel: status === 'pending', started_at: '2026-10-22T06:01:00Z', finished_at: '2026-10-22T06:02:00Z' }
      context.mock.method(globalThis, 'fetch', async () => Response.json(body))
      // When
      const result = await operation(target)
      // Then
      assert.deepEqual(result, body)
    })
  }

  for (const body of [
    {}, { ...receipt, status: 'queued' }, { ...receipt, run_id: '' }, { ...receipt, schedule_id: 4 },
    { ...receipt, requester_id: '' }, { ...receipt, agent_id: null }, { ...receipt, created_at: null },
    { ...receipt, created_at: 'yesterday' }, { ...receipt, started_at: '' }, { ...receipt, finished_at: 42 },
    { ...receipt, can_cancel: false }, { ...receipt, status: 'running' },
  ]) {
    test(`${operation.name} rejects malformed receipt ${JSON.stringify(body)}`, async (context) => {
      // Given
      context.mock.method(globalThis, 'fetch', async () => Response.json(body))
      // When
      const result = operation(target)
      // Then
      await assert.rejects(result, ZodError)
    })
  }

  for (const status of [403, 404, 409, 503]) {
    test(`${operation.name} preserves HTTP ${status} rather than parsing it as a receipt`, async (context) => {
      // Given
      context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Receipt refused' }, { status }))
      // When
      const result = operation(target)
      // Then
      await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === status)
    })
  }

  test(`${operation.name} discards a response aborted before its body completes`, async (context) => {
    // Given
    const controller = new AbortController()
    context.mock.method(globalThis, 'fetch', async () => {
      controller.abort()
      return Response.json(receipt)
    })
    // When
    const result = operation(target, { signal: controller.signal })
    // Then
    await assert.rejects(result, (error: unknown) => error instanceof DOMException && error.name === 'AbortError')
  })
}

test('retrying the same manual intent preserves the exact key after a lost response', async (context) => {
  // Given
  const keys: (string | null)[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    keys.push(new Headers(init.headers).get('Idempotency-Key'))
    if (keys.length === 1) throw new TypeError('Response lost')
    return Response.json(accepted)
  })
  await assert.rejects(runSchedule('agent', 'schedule', 'retained-intent'), TypeError)
  // When
  const result = await runSchedule('agent', 'schedule', 'retained-intent')
  // Then
  assert.deepEqual(keys, ['retained-intent', 'retained-intent'])
  assert.equal(result.run_id, 'durable-run')
})

for (const body of [{}, { ...accepted, run_id: '' }, { ...accepted, status: 'success' }, { ...accepted, schedule_id: 4 }]) {
  test(`manual Run refuses malformed acceptance ${JSON.stringify(body)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When
    const result = runSchedule('agent', 'schedule', 'intent')
    // Then
    await assert.rejects(result, ZodError)
  })
}
