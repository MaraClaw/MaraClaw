import assert from 'node:assert/strict'
import { test } from 'node:test'
import { connectAgentChat } from '../src/lib/chat/ws-client.ts'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { canonicalMessage, canonicalSession, firstSubmission, fixture, secondSubmission, submission } from './socket-fixture.ts'

const receipt = {
  type: 'turn_accepted', client_submission_id: firstSubmission,
  message_id: canonicalMessage, session_id: canonicalSession,
}

for (const malformed of [
  { type: 'turn_accepted' },
  { ...receipt, client_submission_id: undefined },
  { ...receipt, client_submission_id: 'invalid' },
  { ...receipt, session_id: 42 },
  { ...receipt, message_id: undefined },
  { ...receipt, message_id: 'invalid' },
]) {
  test(`malformed acceptance cannot fall through the generic event parser: ${JSON.stringify(malformed)}`, async (t) => {
    const f = fixture()
    const conn = f.connect()
    t.after(conn.close)
    await Promise.resolve()
    f.socket().open()
    conn.send(submission('question'))
    f.socket().frame(JSON.stringify(malformed))
    assert.deepEqual(f.events, [{ type: 'error', content: 'Malformed chat event' }])
  })
}

test('acceptance retires only the matching scoped envelope before reconnect replay', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const conn = f.connect()
  t.after(conn.close)
  await Promise.resolve()
  f.socket().open()
  conn.send(submission('same text'))
  conn.send(submission('same text', secondSubmission))
  f.socket().frame(JSON.stringify({ ...receipt, session_id: secondSubmission }))
  f.socket().frame(JSON.stringify(receipt))
  f.socket().frame(JSON.stringify(receipt))
  f.socket().end(1006)
  t.mock.timers.tick(600)
  await Promise.resolve()
  f.socket().open()
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [submission('same text', secondSubmission)])
  assert.deepEqual(f.events, [receipt])
})

test('lost acknowledgement replays the frozen file envelope after effect recreation', async (t) => {
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const first = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
  const envelope = { ...submission('extracted text'), display_content: 'notes.txt', file_name: 'notes.txt', model_id: 'model-a' }
  await Promise.resolve()
  f.socket().open()
  first.send(envelope)
  const lateFrame = f.socket().onmessage
  envelope.content = 'changed after send'
  first.close()
  const recreated = connectAgentChat('agent-a', canonicalSession, { ...f.handlers, submissions })
  t.after(recreated.close)
  await Promise.resolve()
  lateFrame?.({ data: JSON.stringify(receipt) })
  f.socket().open()
  assert.deepEqual(f.sockets.map((socket) => socket.sent), [[JSON.stringify({ ...envelope, content: 'extracted text' })], [JSON.stringify({ ...envelope, content: 'extracted text' })]])
  assert.deepEqual(f.events, [])
})

test('history identity stops replay even if the acceptance frame was lost', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
  t.after(conn.close)
  await Promise.resolve()
  f.socket().open()
  conn.send(submission('question'))
  submissions.reconcile([{ role: 'user', content: 'question', client_submission_id: firstSubmission, message_id: canonicalMessage }])
  f.socket().end(1006)
  t.mock.timers.tick(600)
  await Promise.resolve()
  f.socket().open()
  conn.send(submission('question'))
  assert.deepEqual(f.socket().sent, [])
})

test('a rejected file waits for explicit same-ID retry and retains every original field', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const conn = f.connect()
  t.after(conn.close)
  await Promise.resolve()
  f.socket().open()
  const envelope = { ...submission('extracted'), file_name: 'file.txt', display_content: 'file.txt' }
  conn.send(envelope)
  f.socket().frame(JSON.stringify({ type: 'turn_rejected', client_submission_id: firstSubmission, reason: 'enqueue_failed', content: 'Retry' }))
  f.socket().end(1006)
  t.mock.timers.tick(600)
  await Promise.resolve()
  f.socket().open()
  assert.deepEqual(f.socket().sent, [])
  conn.send({ ...envelope, content: 'must not replace original', file_name: 'other.txt' })
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [envelope])
})

for (const boundary of ['owner', 'conversation', 'agent'] as const) {
  test(`retired ${boundary} cannot replay or process a late acceptance`, async (t) => {
    const f = fixture()
    const owner = f.store.capture()
    const submissions = createChatSubmissions(owner, 'agent-a', 'thread-a')
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
    await Promise.resolve()
    f.socket().open()
    conn.send(submission('private'))
    const frame = f.socket().onmessage
    if (boundary === 'owner') f.store.beginReplacement(owner, 'token-a')
    else submissions.retire()
    conn.close()
    const next = connectAgentChat(boundary === 'agent' ? 'agent-b' : 'agent-a', boundary === 'conversation' ? 'other' : 'thread-a', { ...f.handlers, submissions })
    t.after(next.close)
    await Promise.resolve()
    frame?.({ data: JSON.stringify(receipt) })
    assert.equal(f.sockets.length, 1)
    assert.deepEqual(submissions.pending(), [])
    assert.deepEqual(f.events, [])
  })
}
