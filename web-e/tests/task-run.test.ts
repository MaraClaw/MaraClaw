import assert from 'node:assert/strict'
import { test } from 'node:test'

import { triggerTask } from '../src/lib/agent-work-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { canRunTask, taskRunErrorMessage } from '../src/lib/task-run.ts'

test('Run is disabled for doing tasks and enabled for idle tasks', () => {
  for (const [status, enabled] of [['doing', false], ['pending', true], ['done', true], [null, true], [undefined, true]] as const) {
    assert.equal(canRunTask(status), enabled)
  }
})

test('a trigger conflict displays the server detail', async (context) => {
  const detail = 'This task is already running. Wait for it to finish before retrying.'
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status: 409 }))
  await assert.rejects(triggerTask('agent', 'task'), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 409)
    assert.equal(taskRunErrorMessage(error), detail)
    return true
  })
})

test('unexpected and server failures display a safe fallback', () => {
  for (const error of [new Error('internal'), new ApiError(503, 'database credentials'), new ApiError(409, '<html>proxy failure')]) {
    assert.equal(taskRunErrorMessage(error), 'Unable to run task')
  }
})
