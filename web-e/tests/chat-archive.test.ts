import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { connectAgentChat } from '../src/lib/chat/ws-client.ts'
import { ChatEventType, archivedSessionId, inboundSchema } from '../src/lib/chat/protocol.ts'
import { canonicalSession, fixture, submission } from './socket-fixture.ts'

test('a structured archived-session error survives the wire without acknowledging pending work', async (t) => {
  // Given
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
  t.after(conn.close)
  await Promise.resolve()
  f.socket().open()
  const envelope = submission('Awaiting acknowledgement')
  conn.send(envelope)
  const frame = { type: ChatEventType.ERROR, content: { reason: 'session_archived', session_id: canonicalSession } }
  // When
  f.socket().frame(JSON.stringify(frame))
  // Then
  assert.deepEqual(f.events, [frame])
  assert.deepEqual(submissions.pending(), [envelope])
  const event = f.events[0]
  assert.ok(event)
  assert.equal(archivedSessionId(event), canonicalSession)
})

for (const content of [
  { reason: 'session_archived', session_id: 'not-a-uuid' },
  { reason: 'session_archived' },
  { reason: 'something_else', session_id: canonicalSession },
]) {
  test('a malformed structured error cannot masquerade as an archived session', () => {
    // Given / When
    const result = inboundSchema.safeParse({ type: 'error', content })
    // Then
    assert.equal(result.success, false)
  })
}

test('a string error remains an ordinary error instead of an archive transition', () => {
  // Given / When
  const event = inboundSchema.parse({ type: 'error', content: 'Unavailable' })
  // Then
  assert.equal(archivedSessionId(event), undefined)
  assert.equal(event.type, ChatEventType.ERROR)
  if (event.type !== ChatEventType.ERROR) throw new Error('Expected error event')
  assert.equal(event.content, 'Unavailable')
})
