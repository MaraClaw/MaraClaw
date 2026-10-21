import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { runSchedule } from '../src/lib/schedules-api.ts'

const accepted = { status: 'triggered', schedule_id: 'schedule', run_id: 'durable-run' } as const

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
