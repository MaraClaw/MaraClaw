import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { getAgentLifecycle } from '../src/lib/agent-lifecycle-api.ts'
import {
  canManageAgent, classifyStartNewContainerError, runtimeLossSummary, startNewContainerErrorMessage,
  startNewContainerFailureMessage, type StartNewContainerFailure,
} from '../src/lib/agent-runtime-loss.ts'
import { getAgent, startNewContainer } from '../src/lib/agents-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { vietnamese } from '../src/lib/i18n/catalog.ts'
import { isTranslationKey } from '../src/lib/i18n/translation.ts'
import { lifecycle } from './agent-lifecycle-fixture.ts'

const id = '11111111-1111-4111-8111-111111111111'
const agent = {
  id, name: 'Assistant', role_description: 'Assistant', status: 'running', creator_id: id,
  autonomy_policy: {}, tokens_used_today: 0, tokens_used_month: 0, created_at: '2026-10-25T08:00:00Z',
}
const detectedAt = '2026-11-13T09:30:00Z'
const engineText = 'The agent still has a guest container; use Start'

for (const reason of ['container_missing', 'image_unavailable'] as const) {
  test(`Given the ${reason} loss reason, When its message is read, Then it is a translated catalog entry`, () => {
    // Given / When
    const message = runtimeLossSummary[reason]
    // Then
    assert.ok(isTranslationKey(message))
    assert.notEqual(vietnamese[message], message)
  })
}

test('Given both loss reasons, When their messages are compared, Then each reason has its own message', () => {
  // Given / When / Then
  assert.notEqual(runtimeLossSummary.container_missing, runtimeLossSummary.image_unavailable)
})

const known: ReadonlyArray<readonly [string, number, unknown, StartNewContainerFailure]> = [
  ['a non-manager', 403, 'Only manager can start agent', 'forbidden'],
  ['incomplete setup with a plain detail', 409, 'Agent setup is incomplete', 'setup_incomplete'],
  ['incomplete setup with a code', 409, { code: 'guest_setup_incomplete', message: engineText }, 'setup_incomplete'],
  ['a container that still exists', 409, { code: 'guest_container_present', message: engineText }, 'container_present'],
  ['an unavailable runtime image', 409, { code: 'guest_image_unavailable', message: engineText }, 'image_unavailable'],
  ['an ineligible agent', 409, { code: 'guest_start_ineligible', message: engineText }, 'start_ineligible'],
  ['exhausted memory capacity', 409, { code: 'guest_memory_capacity_exceeded', message: engineText }, 'capacity_exceeded'],
  ['an unbounded memory limit', 409, { code: 'guest_memory_unbounded', message: engineText }, 'runtime_attention'],
  ['a required recreation', 409, { code: 'guest_recreation_required', message: engineText }, 'runtime_attention'],
  ['a failed start', 502, { code: 'guest_start_failed', message: engineText }, 'start_failed'],
]

for (const [label, status, detail, expected] of known) {
  test(`Given ${label} (${status}), When the error is classified, Then it maps to ${expected}`, () => {
    // Given
    const error = new ApiError(status, detail)
    // When / Then
    assert.equal(classifyStartNewContainerError(error), expected)
  })
}

const unrecognised: ReadonlyArray<readonly [string, unknown]> = [
  ['an unknown code on 409', new ApiError(409, { code: 'guest_unheard_of', message: engineText })],
  ['a known code on the wrong status', new ApiError(500, { code: 'guest_start_failed', message: engineText })],
  ['a plain 409 detail', new ApiError(409, 'Something else is wrong')],
  ['a 502 without a code', new ApiError(502, null)],
  ['a 404', new ApiError(404, 'Agent not found')],
  ['a non-string code', new ApiError(409, { code: 7 })],
  ['a network failure', new TypeError('Failed to fetch')],
  ['a non-error value', 'guest_start_failed'],
]

for (const [label, error] of unrecognised) {
  test(`Given ${label}, When the error is classified, Then the generic fallback applies`, () => {
    // Given / When / Then
    assert.equal(classifyStartNewContainerError(error), 'unknown')
    assert.equal(startNewContainerErrorMessage(error), startNewContainerFailureMessage.unknown)
  })
}

test('Given the engine detail text, When a failure is mapped, Then the message is a catalog entry and never the engine text', () => {
  // Given
  for (const [, status, detail] of known) {
    // When
    const message = startNewContainerErrorMessage(new ApiError(status, detail))
    // Then
    assert.ok(isTranslationKey(message))
    assert.notEqual(message, engineText)
    assert.notEqual(message, detail)
  }
})

test('Given every failure kind, When the messages are compared, Then each is distinct and translated', () => {
  // Given
  const messages = Object.values(startNewContainerFailureMessage)
  // When / Then
  assert.equal(new Set(messages).size, messages.length)
  for (const message of messages) assert.notEqual(vietnamese[message], message)
})

for (const [label, access, creator, userId, allowed] of [
  ['manage access', 'manage', 'other', 'viewer', true],
  ['the creator with use access', 'use', 'creator', 'creator', true],
  ['the creator without an access level', undefined, 'creator', 'creator', true],
  ['use access only', 'use', 'creator', 'viewer', false],
  ['no access level and another creator', null, 'creator', 'viewer', false],
  ['no signed-in user', 'use', 'creator', undefined, false],
] as const) {
  test(`Given ${label}, When manager rights are checked, Then ${allowed ? 'the member can' : 'the member cannot'} start a new container`, () => {
    // Given / When / Then
    assert.equal(canManageAgent({ access_level: access, creator_id: creator }, userId), allowed)
  })
}

test('Given a lost container, When a new one is started, Then the client POSTs without a body and keeps the parsed agent', async (t) => {
  // Given
  let path: RequestInfo | URL | undefined
  let request: RequestInit | undefined
  const started = { ...agent, runtime_loss: null }
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    path = input; request = init; return Response.json(started)
  })
  // When
  const result = await startNewContainer(id)
  // Then
  assert.deepEqual(result, started)
  assert.equal(path, `/api/agents/${id}/start-new-container`)
  assert.equal(request?.method, 'POST')
  assert.equal(request?.body, undefined)
})

test('Given an engine refusal, When a new container is requested, Then the ApiError carries the status and code to classify', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json(
    { detail: { code: 'guest_image_unavailable', message: engineText } }, { status: 409 },
  ))
  // When
  const error = await startNewContainer(id).catch((caught: unknown) => caught)
  // Then
  assert.ok(error instanceof ApiError)
  assert.equal(error.status, 409)
  assert.equal(classifyStartNewContainerError(error), 'image_unavailable')
})

test('Given a runtime loss on the agent, When it is read, Then runtime_loss is preserved with its reason and time', async (t) => {
  // Given
  const lost = { ...agent, runtime_loss: { reason: 'container_missing', detected_at: detectedAt } }
  t.mock.method(globalThis, 'fetch', async () => Response.json(lost))
  // When
  const result = await getAgent(id)
  // Then
  assert.deepEqual(result.runtime_loss, { reason: 'container_missing', detected_at: detectedAt })
})

test('Given an agent without a loss, When it is read, Then runtime_loss is absent or null', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json(agent))
  // When
  const result = await getAgent(id)
  // Then
  assert.equal(result.runtime_loss ?? null, null)
})

test('Given an unknown loss reason, When the agent is read, Then the response fails closed', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...agent, runtime_loss: { reason: 'unknown-contract-value', detected_at: detectedAt } }))
  // When / Then
  await assert.rejects(getAgent(id), ZodError)
})

test('Given a loss in the lifecycle projection, When it is read, Then runtime.loss is preserved and unknown reasons fail closed', async (t) => {
  // Given
  const loss = { reason: 'image_unavailable', detected_at: detectedAt }
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...lifecycle, runtime: { ...lifecycle.runtime, loss } }))
  // When
  const result = await getAgentLifecycle(id)
  // Then
  assert.deepEqual(result.runtime.loss, loss)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...lifecycle, runtime: { ...lifecycle.runtime, loss: { ...loss, reason: 'unknown-contract-value' } } }))
  await assert.rejects(getAgentLifecycle(id), ZodError)
})
