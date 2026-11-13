import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ChatEventType } from '../src/lib/chat/protocol.ts'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import { ControlledSocket, canonicalSession, drain, fixture, firstSubmission, secondSubmission, submission } from './socket-fixture.ts'

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
  const cleared = clear.mock.callCount()
  // When
  f.store.beginReplacement(f.store.capture(), 'token-b')
  // Then
  assert.equal(clear.mock.callCount(), cleared + 1)
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

for (const [random, delay] of [[0, 300], [0.5, 450], [1, 600]] as const) {
  test(`first reconnect waits ${delay}ms when the random source returns ${random}`, async (t) => {
    // Given
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const f = fixture()
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, random: () => random })
    await Promise.resolve()
    // When
    f.socket().end(1006)
    t.mock.timers.tick(delay - 1)
    await Promise.resolve()
    // Then
    assert.equal(f.sockets.length, 1)
    t.mock.timers.tick(1)
    await Promise.resolve()
    assert.equal(f.sockets.length, 2)
    conn.close()
  })
}

test('backoff keeps growing across opens that never reach CONNECTED and resets on CONNECTED', async (t) => {
  // Given
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const conn = connectAgentChat('agent-a', 'thread-a', f.handlers)
  await Promise.resolve()
  const reconnectAfter = async (delay: number) => {
    const before = f.sockets.length
    t.mock.timers.tick(delay - 1)
    await Promise.resolve()
    assert.equal(f.sockets.length, before)
    t.mock.timers.tick(1)
    await Promise.resolve()
    assert.equal(f.sockets.length, before + 1)
  }
  // When / Then
  f.socket().end(1006)
  await reconnectAfter(600)
  f.socket().open()
  f.socket().end(1006)
  await reconnectAfter(1200)
  f.socket().open()
  f.socket().frame(JSON.stringify({ type: 'connected', session_id: canonicalSession }))
  f.socket().end(1006)
  await reconnectAfter(600)
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

const connectedFrame = JSON.stringify({ type: ChatEventType.CONNECTED, session_id: canonicalSession })
const heartbeatFrame = JSON.stringify({ type: ChatEventType.HEARTBEAT })

async function connectedSession(f: ReturnType<typeof fixture>, extra?: Parameters<ReturnType<typeof fixture>['timed']>[0]) {
  const conn = f.timed(extra)
  await drain()
  f.socket().open()
  f.socket().frame(connectedFrame)
  return conn
}

test('socket URL opts in to server heartbeats', async (t) => {
  // Given
  const f = fixture()
  const conn = f.timed()
  t.after(conn.close)
  // When
  await drain()
  // Then
  assert.equal(new URL(f.urls[0] ?? '').searchParams.get('heartbeat'), '1')
})

test('heartbeat frames prove life without reaching onEvent', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  // When
  f.clock.advance(20_000)
  f.socket().frame(heartbeatFrame)
  f.clock.advance(20_000)
  // Then
  assert.equal(f.socket().closes, 0)
  assert.deepEqual(f.events, [{ type: ChatEventType.CONNECTED, session_id: canonicalSession }])
})

test('a silent connected socket is discarded at 25 s and reconnects through backoff without onclose', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  const silent = f.socket()
  // When
  f.clock.advance(24_999)
  assert.equal(silent.closes, 0)
  f.clock.advance(1)
  // Then
  assert.equal(silent.closes, 1)
  assert.deepEqual([silent.onopen, silent.onmessage, silent.onclose], [null, null, null])
  assert.equal(f.states.at(-1), 'reconnecting')
  f.clock.advance(599)
  await drain()
  assert.equal(f.tickets(), 1)
  f.clock.advance(1)
  await drain()
  assert.equal(f.tickets(), 2)
  assert.equal(f.sockets.length, 2)
})

test('frames arriving before each deadline keep the socket alive across many deadlines', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  // When
  for (let index = 0; index < 20; index++) {
    f.clock.advance(20_000)
    f.socket().frame(index % 2 ? heartbeatFrame : '{"type":"chunk","content":"x"}')
  }
  // Then
  assert.equal(f.socket().closes, 0)
  assert.equal(f.tickets(), 1)
})

test('a throttled deadline timer that runs late after recent frames re-arms for the remainder', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  // When: frames keep arriving while the throttled 25 s timer runs 35 s late
  for (let index = 0; index < 3; index++) {
    f.clock.lag(20_000)
    f.socket().frame(heartbeatFrame)
  }
  f.clock.advance(0)
  // Then
  assert.equal(f.socket().closes, 0)
  f.clock.advance(24_999)
  assert.equal(f.socket().closes, 0)
  f.clock.advance(1)
  assert.equal(f.socket().closes, 1)
})

test('a deadline timer waking far late re-checks once so already-queued frames still count', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  // When: after an hour asleep the timer runs before the queued frame is dispatched
  f.clock.lag(3_600_000)
  f.clock.advance(0)
  f.socket().frame(heartbeatFrame)
  f.clock.advance(24_000)
  // Then
  assert.equal(f.socket().closes, 0)
  assert.equal(f.tickets(), 1)
})

test('late close and message events from a discarded socket are ignored', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  const { onclose: lateClose, onmessage: lateMessage } = f.socket()
  f.clock.advance(25_000)
  // When
  lateMessage?.({ data: '{"type":"chunk","content":"late"}' })
  lateClose?.({ code: 1006 })
  await f.elapse(15_000)
  // Then
  assert.equal(f.tickets(), 2)
  assert.equal(f.sockets.length, 2)
  assert.deepEqual(f.events, [{ type: ChatEventType.CONNECTED, session_id: canonicalSession }])
})

test('liveness is armed only after connected and cleared when the socket closes', async () => {
  // Given
  const f = fixture()
  const conn = f.timed()
  await drain()
  f.socket().open()
  // When / Then: before connected only the 15 s connect deadline is armed, never the 25 s liveness timer
  f.clock.advance(14_999)
  assert.equal(f.socket().closes, 0)
  assert.equal(f.clock.pending, 1)
  assert.equal(f.clock.nextDue(), 15_000)
  f.socket().frame(connectedFrame)
  assert.equal(f.clock.pending, 1)
  f.socket().end(1006)
  assert.equal(f.clock.pending, 1)
  conn.close()
  assert.equal(f.clock.pending, 0)
})

test('a ticket request that never settles is aborted at the 15 s connect deadline and retried through backoff', async (t) => {
  // Given
  const f = fixture()
  f.stall(true)
  const conn = f.timed()
  t.after(conn.close)
  await drain()
  const request = f.hung[0]
  assert.ok(request)
  // When
  f.clock.advance(14_999)
  assert.equal(request.signal.aborted, false)
  assert.equal(f.states.at(-1), 'connecting')
  f.clock.advance(1)
  // Then
  assert.equal(request.signal.aborted, true)
  assert.equal(f.states.at(-1), 'reconnecting')
  assert.deepEqual(f.codes, [])
  f.clock.advance(599)
  await drain()
  assert.equal(f.tickets(), 1)
  f.clock.advance(1)
  await drain()
  assert.equal(f.tickets(), 2)
  assert.equal(f.sockets.length, 0)
})

test('a socket that opens but never sends connected is closed client-side at the connect deadline and retried through backoff', async (t) => {
  // Given
  const f = fixture()
  const conn = f.timed()
  t.after(conn.close)
  await drain()
  const silent = f.socket()
  silent.open()
  // When
  f.clock.advance(14_999)
  assert.equal(silent.closes, 0)
  f.clock.advance(1)
  // Then
  assert.equal(silent.closes, 1)
  assert.deepEqual([silent.onopen, silent.onmessage, silent.onclose], [null, null, null])
  assert.equal(f.states.at(-1), 'reconnecting')
  assert.deepEqual(f.codes, [])
  f.clock.advance(599)
  await drain()
  assert.equal(f.tickets(), 1)
  f.clock.advance(1)
  await drain()
  assert.equal(f.tickets(), 2)
  assert.equal(f.sockets.length, 2)
})

test('when every attempt hangs chat suspends by 120 s with no timers or tickets left and retry starts exactly one attempt', async (t) => {
  // Given
  const f = fixture()
  f.stall(true)
  const conn = f.timed()
  t.after(conn.close)
  // When
  await f.elapse(119_999)
  assert.equal(f.states.at(-1), 'reconnecting')
  await f.elapse(1)
  // Then
  assert.equal(f.states.at(-1), 'suspended')
  assert.equal(f.hung.length, f.tickets())
  assert.ok(f.hung.every(({ signal }) => signal.aborted))
  const tickets = f.tickets()
  await f.elapse(3_600_000)
  assert.equal(f.tickets(), tickets)
  assert.equal(f.clock.pending, 0)
  conn.retry()
  conn.retry()
  await drain()
  assert.equal(f.tickets(), tickets + 1)
})

test('a connection that connected before the connect deadline is not discarded while frames keep flowing', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  // When
  for (let index = 0; index < 3; index++) {
    f.clock.advance(10_000)
    f.socket().frame(heartbeatFrame)
  }
  // Then
  assert.equal(f.socket().closes, 0)
  assert.equal(f.tickets(), 1)
  assert.equal(f.states.at(-1), 'connected')
  assert.equal(f.clock.pending, 1)
})

test('a ticket that resolves after its attempt was abandoned creates no second socket', async (t) => {
  // Given
  const f = fixture()
  f.stall(true)
  const conn = f.timed()
  t.after(conn.close)
  await drain()
  f.clock.advance(15_000)
  f.stall(false)
  f.clock.advance(600)
  await drain()
  const current = f.socket()
  assert.equal(f.sockets.length, 1)
  // When
  f.hung[0]?.release()
  await drain()
  // Then
  assert.equal(f.sockets.length, 1)
  assert.equal(f.urls.length, 1)
  assert.equal(current.closes, 0)
  assert.equal(f.states.at(-1), 'reconnecting')
})

for (const race of ['timer first', 'online first', 'suspended'] as const) {
  test(`racing online, backoff timer, visibility, retry and send (${race}) make one ticket request and one socket`, async (t) => {
    // Given
    const f = fixture()
    const conn = await connectedSession(f)
    t.after(conn.close)
    f.socket().end(1006)
    if (race === 'suspended') {
      f.outage(true)
      await f.elapse(120_000)
      assert.equal(f.states.at(-1), 'suspended')
      f.outage(false)
    }
    const tickets = f.tickets()
    const sockets = f.sockets.length
    // When
    if (race === 'timer first') f.clock.advance(600)
    f.network.set(true)
    f.clock.advance(8_000)
    f.visibility.set(true)
    conn.retry()
    conn.send(submission('racing'))
    await drain()
    conn.retry()
    f.network.set(true)
    // Then
    assert.equal(f.tickets(), tickets + 1)
    assert.equal(f.sockets.length, sockets + 1)
  })
}

for (const moment of ['at start', 'while connected'] as const) {
  test(`offline ${moment} requests no ticket until online, then exactly one`, async (t) => {
    // Given
    const f = fixture()
    if (moment === 'at start') f.network.set(false)
    const conn = moment === 'at start' ? f.timed() : await connectedSession(f)
    t.after(conn.close)
    const tickets = f.tickets()
    // When
    f.network.set(false)
    conn.retry()
    conn.send(submission('offline'))
    f.visibility.set(true)
    await f.elapse(600_000)
    // Then
    assert.equal(f.tickets(), tickets)
    assert.equal(f.clock.pending, 0)
    f.network.set(true)
    await drain()
    assert.equal(f.tickets(), tickets + 1)
    assert.deepEqual(f.states, moment === 'at start' ? ['offline', 'connecting'] : ['connecting', 'connected', 'offline', 'reconnecting'])
  })
}

test('an outage past 120 s suspends without timers or tickets and keeps the subscription and ledger', async (t) => {
  // Given
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const conn = await connectedSession(f, { submissions })
  t.after(conn.close)
  f.outage(true)
  f.socket().end(1006)
  conn.send(submission('waiting'))
  // When
  await f.elapse(119_999)
  assert.equal(f.states.at(-1), 'reconnecting')
  await f.elapse(1)
  // Then
  assert.equal(f.states.at(-1), 'suspended')
  const tickets = f.tickets()
  await f.elapse(3_600_000)
  assert.equal(f.tickets(), tickets)
  assert.equal(f.clock.pending, 0)
  assert.deepEqual(submissions.pending(), [submission('waiting')])
  assert.deepEqual([f.network.listeners, f.visibility.listeners], [1, 1])
  f.store.logout(f.store.capture())
  assert.equal(submissions.active, false)
})

for (const trigger of ['online', 'visible', 'send', 'retry'] as const) {
  test(`suspended chat resumes on ${trigger} with one ticket, pending work resent once and onReconnect`, async (t) => {
    // Given
    const f = fixture()
    let reconnects = 0
    const conn = await connectedSession(f, { onReconnect: () => { reconnects++ } })
    t.after(conn.close)
    f.outage(true)
    f.visibility.set(false)
    f.socket().end(1006)
    conn.send(submission('waiting'))
    conn.send({ kind: 'onboarding_trigger' })
    await f.elapse(120_000)
    assert.equal(f.states.at(-1), 'suspended')
    f.outage(false)
    const tickets = f.tickets()
    // When
    switch (trigger) {
      case 'online': f.network.set(true); break
      case 'visible': f.visibility.set(true); break
      case 'send': conn.send(submission('waiting')); break
      case 'retry': conn.retry(); break
      default: trigger satisfies never
    }
    await drain()
    f.socket().open()
    f.socket().frame(connectedFrame)
    // Then
    assert.equal(f.tickets(), tickets + 1)
    assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [submission('waiting'), { kind: 'onboarding_trigger' }])
    assert.equal(reconnects, 1)
    assert.equal(f.states.at(-1), 'connected')
  })
}

for (const code of [4001, 4003]) {
  test(`terminal ${code} on a live session leaves no retry, timer or listener`, async () => {
    // Given
    const f = fixture()
    const conn = await connectedSession(f)
    // When
    f.socket().end(code)
    conn.retry()
    f.network.set(true)
    f.visibility.set(true)
    await f.elapse(600_000)
    // Then
    assert.deepEqual(f.codes, [code])
    assert.equal(f.tickets(), 1)
    assert.equal(f.clock.pending, 0)
    assert.deepEqual([f.network.listeners, f.visibility.listeners], [0, 0])
    assert.deepEqual(f.states, ['connecting', 'connected'])
  })
}

for (const state of ['connecting', 'connected', 'reconnecting', 'offline', 'suspended'] as const) {
  test(`close while ${state} leaves no timer, listener or later ticket request`, async () => {
    // Given
    const f = fixture()
    const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
    const conn = state === 'connecting' || state === 'offline' ? f.timed({ submissions }) : await connectedSession(f, { submissions })
    switch (state) {
      case 'connecting': case 'connected': break
      case 'reconnecting': f.socket().end(1006); break
      case 'offline': f.network.set(false); break
      case 'suspended': f.outage(true); f.socket().end(1006); await f.elapse(120_000); break
      default: state satisfies never
    }
    assert.equal(f.states.at(-1), state)
    // When
    conn.close()
    // Then
    assert.equal(f.clock.pending, 0)
    assert.deepEqual([f.network.listeners, f.visibility.listeners], [0, 0])
    const tickets = f.tickets()
    f.network.set(true)
    f.visibility.set(true)
    conn.retry()
    await f.elapse(600_000)
    assert.equal(f.tickets(), tickets)
    f.store.logout(f.store.capture())
    assert.equal(submissions.active, true)
  })
}

test('connection states follow connect, reconnect, a window reset by connected, suspension and retry', async (t) => {
  // Given
  const f = fixture()
  const conn = await connectedSession(f)
  t.after(conn.close)
  // When
  f.outage(true)
  f.socket().end(1006)
  await f.elapse(100_000)
  f.outage(false)
  await f.elapse(8_000)
  f.socket().open()
  f.socket().frame(connectedFrame)
  f.outage(true)
  f.socket().end(1006)
  await f.elapse(119_999)
  const beforeWindow = f.states.at(-1)
  await f.elapse(1)
  f.outage(false)
  conn.retry()
  await drain()
  f.socket().open()
  f.socket().frame(connectedFrame)
  // Then
  assert.equal(beforeWindow, 'reconnecting')
  assert.deepEqual(f.states, ['connecting', 'connected', 'reconnecting', 'connected', 'reconnecting', 'suspended', 'reconnecting', 'connected'])
})
