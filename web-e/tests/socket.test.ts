import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { connectAgentChat } from '../src/lib/chat/ws-client.ts'
import type { ChatInbound, ChatSocket, ChatTransport } from '../src/lib/chat/ws-client.ts'

class ControlledSocket implements ChatSocket {
  readyState = 0
  onopen: ChatSocket['onopen'] = null
  onmessage: ChatSocket['onmessage'] = null
  onclose: ChatSocket['onclose'] = null
  readonly sent: string[] = []
  closes = 0
  send(data: string) { this.sent.push(data) }
  close() { this.closes++; this.readyState = 3 }
  open() { this.readyState = 1; this.onopen?.() }
  frame(data: string) { this.onmessage?.({ data }) }
  end(code: number) { this.readyState = 3; this.onclose?.({ code }) }
}

function fixture() {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({
    read: () => token, write: (value) => { token = value }, clear: () => { token = null },
  })
  const sockets: ControlledSocket[] = []
  const events: ChatInbound[] = []
  const codes: number[] = []
  const transport: ChatTransport = {
    language: 'en', url: (path) => `ws://example.test${path}`,
    connect: () => {
      const socket = new ControlledSocket()
      sockets.push(socket)
      return socket
    },
  }
  const handlers = { sessionStore: store, transport, onEvent: (event: ChatInbound) => events.push(event),
    onClose: (code: number) => codes.push(code) }
  const connect = () => connectAgentChat('agent-a', 'token-a', 'thread-a', handlers)
  const socket = () => {
    const latest = sockets.at(-1)
    assert.ok(latest)
    return latest
  }
  return { store, sockets, events, codes, handlers, connect, socket, token: () => token }
}

test('queued sends flush in order when the current socket opens', () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  conn.send({ content: 'first' })
  conn.send({ content: 'second' })
  // When
  f.socket().open()
  // Then
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [{ content: 'first' }, { content: 'second' }])
  conn.close()
})

for (const replacement of ['token-b', 'token-a', null]) {
  test(`pending sends and late frames are suppressed when owner changes to ${replacement}`, () => {
    // Given
    const f = fixture()
    const conn = f.connect()
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

test('reconnect timer is cleared and already queued callback is fenced when ownership changes', (t) => {
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

test('transient disconnect reconnects with pending sends when the owner remains current', (t) => {
  // Given
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const conn = f.connect()
  const old = f.socket()
  old.end(1006)
  conn.send({ content: 'retry' })
  // When
  t.mock.timers.tick(600)
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
  test(`terminal ${code} closes once and ${code === 4001 ? 'expires' : 'retains'} matching auth`, (t) => {
    // Given
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const f = fixture()
    let tokenInCallback: string | null = null
    const conn = connectAgentChat('agent-a', 'token-a', undefined, {
      ...f.handlers,
      onClose: (value) => { tokenInCallback = f.token(); f.codes.push(value) },
    })
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

test('4001 callback replacement cannot be expired by the retiring socket', () => {
  // Given
  const f = fixture()
  connectAgentChat('agent-a', 'token-a', undefined, {
    ...f.handlers, onClose: () => { f.store.beginReplacement(f.store.capture(), 'token-b') },
  })
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
  const conn = connectAgentChat('agent-a', 'token-a', undefined, { ...f.handlers, owner })
  conn.send({ content: 'stale' })
  // Then
  assert.equal(f.sockets.length, 0)
})

test('ownership is captured before transport creation even when creation replaces the owner', () => {
  // Given
  const f = fixture()
  const old = new ControlledSocket()
  // When
  const conn = connectAgentChat('agent-a', 'token-a', undefined, {
    ...f.handlers, transport: { ...f.handlers.transport, connect: () => {
      f.store.beginReplacement(f.store.capture(), 'token-b')
      return old
    } },
  })
  // Then
  assert.equal(old.closes, 1)
  conn.send({ content: 'stale' })
  assert.deepEqual(old.sent, [])
})

test('malformed frames become error events when the socket is current', () => {
  // Given
  const f = fixture()
  const conn = f.connect()
  // When
  f.socket().frame('not json')
  // Then
  assert.equal(f.events[0]?.type, 'error')
  conn.close()
})
