import assert from 'node:assert/strict'
import { test } from 'node:test'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import { ControlledSocket, fixture } from './socket-fixture.ts'

test('queued sends flush in order when the current socket opens', async () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  conn.send({ content: 'first' })
  conn.send({ content: 'second' })
  await Promise.resolve()
  // When
  f.socket().open()
  // Then
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [{ content: 'first' }, { content: 'second' }])
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
    conn.send({ content: 'queued' })
    // When
    if (replacement) f.store.beginReplacement(f.store.capture(), replacement)
    else f.store.logout(f.store.capture())
    // Then: shutdown is synchronous, before React cleanup or a socket close event.
    assert.equal(old.closes, 1)
    old.readyState = 1
    open?.()
    message?.({ data: '{"type":"chunk","content":"obsolete"}' })
    close?.({ code: 4001 })
    conn.send({ content: 'late' })
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
  conn.send({ content: 'reconnect-pending' })
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
  conn.send({ content: 'retry' })
  // When
  t.mock.timers.tick(600)
  await Promise.resolve()
  f.socket().open()
  // Then
  assert.equal(f.sockets.length, 2)
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [{ content: 'retry' }])
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
    conn.send({ content: 'pending' })
    // When
    old.end(code)
    // Then
    assert.equal(tokenInCallback, 'token-a')
    assert.equal(f.token(), code === 4001 ? null : 'token-a')
    assert.deepEqual(f.codes, [code])
    old.open()
    old.frame('{"type":"done","content":"late"}')
    old.end(code)
    conn.send({ content: 'late' })
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
  conn.send({ content: 'stale' })
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
  conn.send({ content: 'stale' })
  assert.deepEqual(old.sent, [])
})

test('malformed frames become error events when the socket is current', async () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  await Promise.resolve()
  // When
  f.socket().frame('not json')
  // Then
  assert.equal(f.events[0]?.type, 'error')
  conn.close()
})

test('persisted identities survive decoding while legacy events remain valid', async () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  await Promise.resolve()
  const done = { type: 'done', role: 'assistant', content: 'same', message_id: 'parent', entry_id: 'parent' }
  // When
  f.socket().frame(JSON.stringify(done))
  f.socket().frame('{"type":"done","content":"legacy"}')
  f.socket().frame('{"type":"done","message_id":42}')
  // Then
  assert.deepEqual(f.events, [done, { type: 'done', content: 'legacy' }, { type: 'error', content: 'Malformed chat event' }])
  conn.close()
})

test('only current reconnect opens notify and only unsent payloads flush', async (t) => {
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
  conn.send({ content: 'already sent' })
  first.end(1006)
  conn.send({ content: 'unsent' })
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
  assert.deepEqual(current.sent.map((data) => JSON.parse(data)), [{ content: 'unsent' }])
  assert.deepEqual(first.sent.map((data) => JSON.parse(data)), [{ content: 'already sent' }])
  conn.close()
})

for (const reason of ['quota_exceeded', 'agent_expired', 'no_company_model', 'enqueue_failed']) {
  test(`turn rejection ${reason} decodes without becoming a completion or closing the socket`, async (t) => {
    // Given
    const f = fixture()
    const conn = f.connect()
    t.after(() => conn.close())
    await Promise.resolve()
    f.socket().open()
    const frame = { type: 'turn_rejected', reason, content: 'Message not accepted.' }
    // When
    f.socket().frame(JSON.stringify(frame))
    // Then
    assert.deepEqual(f.events, [frame])
    const event = f.events[0]
    assert.ok(event && isTurnRejected(event))
    assert.equal(event.reason, reason)
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
    assert.deepEqual(f.events, [{ type: 'error', content: 'Malformed chat event' }])
  })
}
