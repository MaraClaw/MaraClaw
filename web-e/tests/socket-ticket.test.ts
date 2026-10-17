import assert from 'node:assert/strict'
import { test } from 'node:test'
import { connectAgentChat } from '../src/lib/chat/ws-client.ts'
import type { ChatTicket } from '../src/lib/auth-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { fixture } from './socket-fixture.ts'

for (const code of [4002, 1011, 1006]) {
  test(`close ${code} issues a fresh opaque ticket and uses the canonical session`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const f = fixture()
    const conn = f.connect()
    await Promise.resolve()
    f.socket().end(code)
    t.mock.timers.tick(600)
    await Promise.resolve()
    assert.equal(f.urls.length, 2)
    assert.deepEqual(f.urls.map((url) => [...new URL(url).searchParams]), [
      [['ticket', 'opaque-1'], ['session_id', 'canonical-thread'], ['lang', 'en']],
      [['ticket', 'opaque-2'], ['session_id', 'canonical-thread'], ['lang', 'en']],
    ])
    conn.close()
  })
}

for (const boundary of ['dispose', 'logout', 'replacement', 'same-token replacement'] as const) {
  for (const outcome of ['success', '401', '403', 'network'] as const) {
    test(`late ticket ${outcome} is inert after ${boundary}`, async () => {
      const f = fixture()
      const pending = Promise.withResolvers<ChatTicket>()
      let signal: AbortSignal | undefined
      const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers,
        issueTicket: (_body, owner, abort) => {
          assert.equal(owner, f.store.capture())
          signal = abort
          return pending.promise
        },
      })
      conn.send({ content: 'discard me' })
      switch (boundary) {
        case 'dispose': conn.close(); break
        case 'logout': f.store.logout(f.store.capture()); break
        case 'replacement': f.store.beginReplacement(f.store.capture(), 'token-b'); break
        case 'same-token replacement': f.store.beginReplacement(f.store.capture(), 'token-a'); break
        default: boundary satisfies never
      }
      assert.equal(signal?.aborted, true)
      if (outcome === 'success') pending.resolve({ ticket: 'late', session_id: 'old-thread' })
      else pending.reject(outcome === 'network' ? new TypeError('offline') : new ApiError(Number(outcome), 'late'))
      await Promise.resolve()
      assert.equal(f.sockets.length, 0)
      assert.deepEqual(f.codes, [])
      assert.deepEqual(f.events, [])
      assert.equal(f.token(), boundary === 'logout' ? null : boundary === 'replacement' ? 'token-b' : 'token-a')
    })
  }
}

for (const status of [401, 403]) {
  test(`current HTTP ${status} is terminal and ${status === 401 ? 'expires' : 'preserves'} auth`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const f = fixture()
    let requests = 0
    const conn = connectAgentChat('agent-a', undefined, { ...f.handlers,
      issueTicket: async () => { requests++; throw new ApiError(status, 'denied') },
    })
    await Promise.resolve()
    t.mock.timers.tick(8000)
    assert.equal(requests, 1)
    assert.deepEqual(f.codes, [status === 401 ? 4001 : 4003])
    assert.equal(f.token(), status === 401 ? null : 'token-a')
    assert.equal(f.sockets.length, 0)
    conn.close()
  })
}

for (const failure of [new ApiError(503, 'unavailable'), new TypeError('offline')]) {
  test(`ticket ${failure.name} retries with bounded backoff without a socket fallback`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const f = fixture()
    let requests = 0
    const conn = connectAgentChat('agent-a', 'requested-thread', { ...f.handlers,
      issueTicket: async (body) => {
        assert.deepEqual(body, { agent_id: 'agent-a', session_id: 'requested-thread' })
        requests++
        if (requests < 7) throw failure
        return { ticket: 'recovered', session_id: 'resolved-thread' }
      },
    })
    await Promise.resolve()
    for (const delay of [600, 1200, 2400, 4800, 8000, 8000]) {
      const before = requests
      t.mock.timers.tick(delay - 1)
      assert.equal(requests, before)
      assert.equal(f.sockets.length, 0)
      t.mock.timers.tick(1)
      await Promise.resolve()
      assert.equal(requests, before + 1)
    }
    assert.equal(new URL(f.urls[0] ?? '').searchParams.get('ticket'), 'recovered')
    conn.close()
  })
}

for (const outcome of ['success', 'error'] as const) {
  test(`a superseded issuance ${outcome} cannot replace the newer attempt`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    let retry: (() => void) | undefined
    const original = globalThis.setTimeout
    t.mock.method(globalThis, 'setTimeout', (callback: () => void, delay?: number) => {
      retry = callback
      return original(callback, delay)
    })
    const f = fixture()
    const old = Promise.withResolvers<ChatTicket>()
    const latest = Promise.withResolvers<ChatTicket>()
    const signals: AbortSignal[] = []
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers,
      issueTicket: (_body, _owner, signal) => {
        signals.push(signal)
        if (signals.length === 1) return Promise.resolve({ ticket: 'initial', session_id: 'thread-a' })
        return signals.length === 2 ? old.promise : latest.promise
      },
    })
    await Promise.resolve()
    f.socket().end(1006)
    assert.ok(retry)
    retry()
    retry()
    assert.equal(signals[1]?.aborted, true)
    latest.resolve({ ticket: 'latest', session_id: 'current-thread' })
    await Promise.resolve()
    if (outcome === 'success') old.resolve({ ticket: 'superseded', session_id: 'old-thread' })
    else old.reject(new ApiError(401, 'obsolete'))
    await Promise.resolve()
    assert.equal(f.sockets.length, 2)
    assert.equal(new URL(f.urls[1] ?? '').searchParams.get('ticket'), 'latest')
    assert.equal(f.token(), 'token-a')
    assert.deepEqual(f.codes, [1006])
    conn.close()
  })
}
