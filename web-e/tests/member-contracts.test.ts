import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'
import { getAgent, listAgents } from '../src/lib/agents-api.ts'
import { createTask, listFocus, listTasks } from '../src/lib/agent-work-api.ts'
import { getChannel } from '../src/lib/channels-api.ts'
import { ApiError } from '../src/lib/http.ts'

const id = '11111111-1111-4111-8111-111111111111'
const createdAt = '2026-10-25T08:00:00Z'
const agent = {
  id, name: 'Assistant', role_description: 'Assistant', status: 'stopped', creator_id: id,
  autonomy_policy: {}, tokens_used_today: 0, tokens_used_month: 0, created_at: createdAt,
}
const task = {
  id, agent_id: id, title: 'Review', type: 'todo', status: 'pending', priority: 'medium',
  assignee: 'agent', created_by: id, created_at: createdAt, updated_at: createdAt,
}

for (const field of ['status', 'agent_type', 'access_mode', 'company_access_level', 'access_level', 'access_reason']) {
  test(`agent HTTP parsing rejects an unknown ${field} instead of accepting a string`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ...agent, [field]: 'unknown-contract-value' }))
    // When / Then
    await assert.rejects(getAgent(id), ZodError)
  })
}

for (const access_reason of ['creator', 'administrator', 'company', 'direct', 'department', null]) {
  test(`agent list and detail preserve the server-derived ${access_reason} explanation`, async (t) => {
    // Given
    const payload = { ...agent, access_reason }
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => Response.json(String(input).endsWith('/agents/') ? [payload] : payload))
    // When
    const [detail, list] = await Promise.all([getAgent(id), listAgents()])
    // Then
    assert.equal(detail.access_reason, access_reason)
    assert.equal(list[0]?.access_reason, access_reason)
  })
}

test('agent parsing preserves the canonical runtime identifier alongside canonical access and status values', async (t) => {
  // Given
  const payload = { ...agent, agent_type: 'openclaw', access_mode: 'company', company_access_level: 'use' }
  t.mock.method(globalThis, 'fetch', async () => Response.json(payload))
  // When
  const result = await getAgent(id)
  // Then
  assert.deepEqual(result, payload)
})

test('agent collections parse every item rather than trusting a type-only array', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json([agent, { ...agent, status: 'unknown-contract-value' }]))
  // When / Then
  await assert.rejects(listAgents(), ZodError)
})

for (const field of ['type', 'status', 'priority']) {
  test(`task HTTP parsing rejects an unknown ${field} on collections`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json([{ ...task, [field]: 'unknown-contract-value' }]))
    // When / Then
    await assert.rejects(listTasks(id), ZodError)
  })
}

test('task creation parses its response instead of only parsing collection responses', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...task, priority: 'unknown-contract-value' }))
  // When / Then
  await assert.rejects(createTask(id, { title: 'Review' }), ZodError)
})

test('task collections accept the canonical task vocabulary', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json([task]))
  // When
  const result = await listTasks(id)
  // Then
  assert.deepEqual(result, [task])
})

for (const field of ['kind', 'status']) {
  test(`focus collections reject an unsupported ${field}`, async (t) => {
    // Given
    const focus = { id, agent_id: id, key: 'review', description: 'Review', kind: 'normal', status: 'in_progress', sort_order: 0, source: 'member' }
    t.mock.method(globalThis, 'fetch', async () => Response.json([{ ...focus, [field]: 'unknown-contract-value' }]))
    // When / Then
    await assert.rejects(listFocus(id), ZodError)
  })
}

test('channel responses cannot hide an unsupported channel type as missing configuration', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    id, agent_id: id, channel_type: 'unknown-contract-value', is_configured: true, is_connected: false,
  }))
  // When / Then
  await assert.rejects(getChannel(id, 'channel'), ZodError)
})

test('channel HTTP failures remain failures instead of pretending the channel is unconfigured', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  // When / Then
  await assert.rejects(getChannel(id, 'channel'), ApiError)
})

test('a missing channel configuration remains an explicit null result', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Not configured' }, { status: 404 }))
  // When
  const result = await getChannel(id, 'channel')
  // Then
  assert.equal(result, null)
})
