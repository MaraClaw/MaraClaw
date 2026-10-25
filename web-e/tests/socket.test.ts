import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ChatEventType } from '../src/lib/chat/protocol.ts'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import { ControlledSocket, fixture, firstSubmission, secondSubmission, submission } from './socket-fixture.ts'

test('queued sends flush in order when the current socket opens', async () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  conn.send(submission('first'))
  conn.send(submission('second', secondSubmission))
  await Promise.resolve()
  // When
  f.socket().open()
  // Then
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [submission('first'), submission('second', secondSubmission)])
  conn.close()
})

for (const replacement of ['token-b', 'token-a', null]) {
  test(`pending sends and late frames are suppressed when owner changes to ${replacement}`, async () => {
    // Given
    const f = fixture()
    const conn = f.connect()
    await Promise.resolve()
    const old = f.socket()
    const open = old.onopen
    const message = old.onmessage
    const close = old.onclose
    conn.send(submission('queued'))
    // When
    if (replacement) f.store.beginReplacement(f.store.capture(), replacement)
    else f.store.logout(f.store.capture())
    // Then: shutdown is synchronous, before React cleanup or a socket close event.
    assert.equal(old.closes, 1)
    old.readyState = 1
    open?.()
    message?.({ data: '{"type":"chunk","content":"obsolete"}' })
    close?.({ code: 4001 })
    conn.send(submission('late'))
    conn.close()
    conn.close()
    assert.deepEqual(old.sent, [])
    assert.deepEqual(f.events, [])
    assert.deepEqual(f.codes, [])
    assert.equal(f.token(), replacement)
    assert.equal(old.closes, 1)
  })
}

test('reconnect timer is cleared and already queued callback is fenced when ownership changes', async (t) => {
  // Given
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const original = globalThis.setTimeout
  let queued: (() => void) | undefined
  t.mock.method(globalThis, 'setTimeout', (callback: () => void, delay?: number) => {
    queued = callback
    return original(callback, delay)
  })
  const clear = t.mock.method(globalThis, 'clearTimeout')
  const f = fixture()
  const conn = f.connect()
  await Promise.resolve()
  f.socket().end(1006)
  conn.send(submission('reconnect-pending'))
  assert.ok(queued)
  // When
  f.store.beginReplacement(f.store.capture(), 'token-b')
  // Then
  assert.equal(clear.mock.callCount(), 1)
  t.mock.timers.tick(8000)
  queued()
  assert.equal(f.sockets.length, 1)
  assert.deepEqual(f.socket().sent, [])
})

test('transient disconnect reconnects with pending sends when the owner remains current', async (t) => {
  // Given
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const conn = f.connect()
  await Promise.resolve()
  const old = f.socket()
  old.end(1006)
  conn.send(submission('retry'))
  // When
  t.mock.timers.tick(600)
  await Promise.resolve()
  f.socket().open()
  // Then
  assert.equal(f.sockets.length, 2)
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [submission('retry')])
  old.frame('{"type":"chunk","content":"old transport"}')
  old.end(4001)
  assert.deepEqual(f.events, [])
  assert.equal(f.token(), 'token-a')
  conn.close()
})

for (const code of [4001, 4003]) {
  test(`terminal ${code} closes once and ${code === 4001 ? 'expires' : 'retains'} matching auth`, async (t) => {
    // Given
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const f = fixture()
    let tokenInCallback: string | null = null
    const conn = connectAgentChat('agent-a', undefined, {
      ...f.handlers,
      onClose: (value) => { tokenInCallback = f.token(); f.codes.push(value) },
    })
    await Promise.resolve()
    const old = f.socket()
    conn.send(submission('pending'))
    // When
    old.end(code)
    // Then
    assert.equal(tokenInCallback, 'token-a')
    assert.equal(f.token(), code === 4001 ? null : 'token-a')
    assert.deepEqual(f.codes, [code])
    old.open()
    old.frame('{"type":"done","content":"late"}')
    old.end(code)
    conn.send(submission('late'))
    t.mock.timers.tick(8000)
    assert.equal(f.sockets.length, 1)
    assert.deepEqual(old.sent, [])
    assert.deepEqual(f.events, [])
    assert.deepEqual(f.codes, [code])
    conn.close()
  })
}

test('4001 callback replacement cannot be expired by the retiring socket', async () => {
  // Given
  const f = fixture()
  connectAgentChat('agent-a', undefined, {
    ...f.handlers, onClose: () => { f.store.beginReplacement(f.store.capture(), 'token-b') },
  })
  await Promise.resolve()
  // When
  f.socket().end(4001)
  // Then
  assert.equal(f.token(), 'token-b')
})

test('explicit obsolete owner never creates a socket', () => {
  // Given
  const f = fixture()
  const owner = f.store.capture()
  f.store.beginReplacement(owner, 'token-a')
  // When
  const conn = connectAgentChat('agent-a', undefined, { ...f.handlers, owner })
  conn.send(submission('stale'))
  // Then
  assert.equal(f.sockets.length, 0)
})

test('ownership is captured before transport creation even when creation replaces the owner', async () => {
  // Given
  const f = fixture()
  const old = new ControlledSocket()
  // When
  const conn = connectAgentChat('agent-a', undefined, {
    ...f.handlers, transport: { ...f.handlers.transport, connect: () => {
      f.store.beginReplacement(f.store.capture(), 'token-b')
      return old
    } },
  })
  await Promise.resolve()
  // Then
  assert.equal(old.closes, 1)
  conn.send(submission('stale'))
  assert.deepEqual(old.sent, [])
})

for (const frame of ['not json', '{"type":"chunk","content":42}', '{"type":"unknown","content":"ignored"}']) {
  test(`malformed frame ${frame} becomes an error event when the socket is current`, async (t) => {
    // Given
    const f = fixture()
    const conn = f.connect()
    t.after(conn.close)
    await Promise.resolve()
    // When
    f.socket().frame(frame)
    // Then
    assert.deepEqual(f.events, [{ type: ChatEventType.ERROR, content: 'Malformed chat event' }])
  })
}

test('persisted identities survive decoding while legacy events remain valid', async () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  await Promise.resolve()
  const done = { type: 'done', role: 'assistant', content: 'same', message_id: '44444444-4444-4444-8444-444444444444', entry_id: '44444444-4444-4444-8444-444444444444' }
  // When
  f.socket().frame(JSON.stringify(done))
  f.socket().frame('{"type":"done","content":"legacy"}')
  f.socket().frame('{"type":"done","message_id":42}')
  // Then
  assert.deepEqual(f.events, [done, { type: 'done', content: 'legacy' }, { type: ChatEventType.ERROR, content: 'Malformed chat event' }])
  conn.close()
})

test('only current reconnect opens notify and all unacknowledged payloads replay', async (t) => {
  // Given
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  let refreshes = 0
  const conn = connectAgentChat('agent-a', 'thread-a', {
    ...f.handlers, onReconnect: () => { refreshes++ },
  })
  await Promise.resolve()
  const first = f.socket()
  first.open()
  conn.send(submission('already sent'))
  first.end(1006)
  conn.send(submission('unsent', secondSubmission))
  // When
  t.mock.timers.tick(600)
  await Promise.resolve()
  const current = f.socket()
  current.open()
  first.open()
  f.store.beginReplacement(f.store.capture(), 'token-b')
  current.open()
  // Then
  assert.equal(refreshes, 1)
  assert.deepEqual(current.sent.map((data) => JSON.parse(data)), [submission('already sent'), submission('unsent', secondSubmission)])
  assert.deepEqual(first.sent.map((data) => JSON.parse(data)), [submission('already sent')])
  conn.close()
})

for (const reason of ['quota_exceeded', 'agent_expired', 'no_company_model', 'enqueue_failed', 'invalid_submission_id', 'submission_conflict']) {
  test(`turn rejection ${reason} decodes without becoming a completion or closing the socket`, async (t) => {
    // Given
    const f = fixture()
    const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
    t.after(() => conn.close())
    await Promise.resolve()
    f.socket().open()
    const rejected = submission('Rejected')
    const waiting = submission('Still pending', secondSubmission)
    conn.send(rejected)
    conn.send(waiting)
    const frame = { type: ChatEventType.TURN_REJECTED, ...(reason === 'invalid_submission_id' ? {} : { client_submission_id: firstSubmission }), reason, content: 'Message not accepted.' }
    // When
    f.socket().frame(JSON.stringify(frame))
    // Then
    assert.deepEqual(f.events, [frame])
    const event = f.events[0]
    assert.ok(event && isTurnRejected(event))
    assert.equal(event.reason, reason)
    assert.deepEqual(submissions.pending(), reason === 'invalid_submission_id' ? [rejected, waiting] : [waiting])
    assert.equal(f.socket().readyState, 1)
    assert.deepEqual(f.codes, [])
  })
}

for (const frame of [
  { type: 'turn_rejected', content: 'Missing reason' },
  { type: 'turn_rejected', reason: 'unknown', content: 'Unknown reason' },
  { type: 'turn_rejected', reason: 'quota_exceeded' },
  { type: 'turn_rejected', reason: 'quota_exceeded', content: 42 },
]) {
  test(`malformed rejection ${JSON.stringify(frame)} remains a protocol error`, async (t) => {
    // Given
    const f = fixture()
    const conn = f.connect()
    t.after(() => conn.close())
    await Promise.resolve()
    // When
    f.socket().frame(JSON.stringify(frame))
    // Then
    assert.deepEqual(f.events, [{ type: ChatEventType.ERROR, content: 'Malformed chat event' }])
  })
}
