import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { listChatHistoryPage } from '../src/lib/chat-history-api.ts'
import type { ChatJsonValue } from '../src/lib/chat/message.ts'
import { mergeServerHistory, type ChatLine } from '../src/lib/chat/transcript.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { historyCursor, historyPage, historyTarget } from './chat-history-fixture.ts'

for (const cursor of [null, historyCursor]) {
  test(`chat history requests a bounded scoped GET ${cursor === null ? 'without' : 'with'} an opaque cursor`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, `/api/agents/${historyTarget.agentId}/sessions/${historyTarget.sessionId}/messages/page`)
      assert.deepEqual([...url.searchParams], [['limit', '50'], ...(cursor === null ? [] : [['cursor', cursor]])])
      assert.equal(init.method, 'GET')
      assert.equal(init.body, undefined)
      assert.equal(init.signal, signal)
      return Response.json(historyPage(10, 60, historyCursor))
    })
    // When
    const page = await listChatHistoryPage(historyTarget, { cursor, signal, owner: authSession.capture() })
    // Then
    assert.deepEqual(page, historyPage(10, 60, historyCursor))
  })
}

test('chat history preserves attachment and receipt identities and strips unknown wire fields', async (context) => {
  // Given
  const row = { entry_id: 'entry', message_id: 'parent', client_submission_id: 'submission', role: 'user', content: '',
    attachments: [{ path: 'uploads/opaque.pdf', name: 'Report.pdf' }] }
  context.mock.method(globalThis, 'fetch', async () => Response.json({
    items: [{ ...row, internal_payload: 'private' }], next_cursor: null, has_more: false,
  }))
  // When
  const page = await listChatHistoryPage(historyTarget)
  // Then
  assert.deepEqual(page.items, [row])
})

const toolArguments: readonly ChatJsonValue[] = [
  { query: 'report', filters: { active: true, count: 3, omitted: null }, scopes: ['team', { id: 7 }] },
  [null, false, 0, ''], null, true, 42, 'query',
]
for (const toolArgs of toolArguments) {
  test(`chat history retains tool and participant metadata with JSON arguments ${JSON.stringify(toolArgs)}`, async (context) => {
    // Given
    const row = { entry_id: 'parent:1', message_id: 'parent', role: 'tool_call', content: '',
      toolName: 'lookup', toolArgs, toolStatus: 'done', toolResult: 'Found', toolThinking: 'Search terms',
      thinking: 'Planning', sender_name: 'Research assistant', participant_id: 'participant' }
    context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [row], next_cursor: null, has_more: false }))
    // When
    const page = await listChatHistoryPage(historyTarget)
    const transcript: readonly ChatLine[] = mergeServerHistory(page.items, [])
    // Then
    assert.deepEqual(transcript, [{ ...row, fromHistory: true }])
  })
}

for (const body of [
  [], {}, { items: [], next_cursor: null, has_more: true }, { items: [], next_cursor: '', has_more: true },
  { items: [], next_cursor: 'cursor', has_more: false }, { items: null, next_cursor: null, has_more: false },
  { items: [{ entry_id: 'id', role: 'assistant', content: 12 }], next_cursor: null, has_more: false },
  { items: [{ entry_id: '', role: 'assistant', content: '' }], next_cursor: null, has_more: false },
  { items: [{ entry_id: 'id', role: 'user', content: '', attachments: [{ path: '', name: 'report' }] }], next_cursor: null, has_more: false },
  { items: [{ entry_id: 'id', role: 'tool_call', content: '', toolThinking: 4 }], next_cursor: null, has_more: false },
  { items: [{ entry_id: 'id', role: 'assistant', content: '', sender_name: {} }], next_cursor: null, has_more: false },
  { items: [{ entry_id: 'id', role: 'assistant', content: '', participant_id: null }], next_cursor: null, has_more: false },
]) {
  test(`chat history rejects malformed page ${JSON.stringify(body)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(listChatHistoryPage(historyTarget), ZodError)
  })
}

test('chat history preserves an older-page HTTP failure instead of returning empty history', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({}, { status: 503 }))
  // When / Then
  await assert.rejects(listChatHistoryPage(historyTarget, { cursor: historyCursor }), (error: unknown) => error instanceof ApiError && error.status === 503)
})

test('chat history discards a response after owner retirement', async (context) => {
  // Given
  const owner = authSession.capture()
  context.mock.method(globalThis, 'fetch', async () => { authSession.logout(owner); return Response.json(historyPage(0, 50)) })
  // When / Then
  await assert.rejects(listChatHistoryPage(historyTarget, { owner }), StaleSessionError)
})

test('chat history discards an aborted page response', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(historyPage(0, 50)) })
  // When / Then
  await assert.rejects(listChatHistoryPage(historyTarget, { signal: controller.signal }), { name: 'AbortError' })
})
