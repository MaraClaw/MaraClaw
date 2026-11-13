import assert from 'node:assert/strict'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { connectAgentChat } from '../src/lib/chat/ws-client.ts'
import type { issueChatTicket } from '../src/lib/auth-api.ts'
import type { ChatConnectionState, ChatInbound, ChatSocket, ChatTransport } from '../src/lib/chat/ws-client.ts'
import type { ChatSubmission } from '../src/lib/chat/protocol.ts'

export const firstSubmission = '11111111-1111-4111-8111-111111111111'
export const secondSubmission = '22222222-2222-4222-8222-222222222222'
export const canonicalSession = '33333333-3333-4333-8333-333333333333'
export const canonicalMessage = '44444444-4444-4444-8444-444444444444'
export const submission = (content: string, client_submission_id = firstSubmission): ChatSubmission => ({ client_submission_id, content })

export class ControlledSocket implements ChatSocket {
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

/** Browser-style boolean source (online, visible); `set` notifies even when the value is unchanged. */
export function signal(initial: boolean) {
  let value = initial
  const listeners = new Set<() => void>()
  return {
    current: () => value,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    set(next: boolean) { value = next; for (const listener of [...listeners]) listener() },
    get listeners() { return listeners.size },
  }
}

/** Manual clock: `advance` fires due timers at their due time; `lag` moves time without firing (throttled or sleeping tab). */
export function manualClock() {
  let time = 0
  let ids = 0
  const timers = new Map<number, { readonly due: number; readonly callback: () => void }>()
  const earliest = () => [...timers].reduce<[number, { readonly due: number; readonly callback: () => void }] | undefined>(
    (first, entry) => (first && first[1].due <= entry[1].due ? first : entry), undefined)
  return {
    now: () => time,
    schedule: (callback: () => void, delay: number) => {
      const id = ids++
      timers.set(id, { due: time + delay, callback })
      return () => { timers.delete(id) }
    },
    advance(ms: number) {
      const end = time + ms
      for (let next = earliest(); next && next[1].due <= end; next = earliest()) {
        timers.delete(next[0])
        time = Math.max(time, next[1].due)
        next[1].callback()
      }
      time = end
    },
    lag(ms: number) { time += ms },
    nextDue: () => earliest()?.[1].due,
    get pending() { return timers.size },
  }
}

/** Lets resolved ticket promises and their continuations run; not a time-based wait. */
export const drain = () => new Promise<void>((resolve) => { setImmediate(resolve) })

export function fixture() {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({
    read: () => token, write: (value) => { token = value }, clear: () => { token = null },
  })
  const sockets: ControlledSocket[] = []
  const events: ChatInbound[] = []
  const codes: number[] = []
  const urls: string[] = []
  const states: ChatConnectionState[] = []
  const network = signal(true)
  const visibility = signal(true)
  const clock = manualClock()
  let tickets = 0
  let reachable = true
  let stalling = false
  const hung: { readonly signal: AbortSignal; readonly release: () => void }[] = []
  const transport: ChatTransport = {
    language: 'en', url: (path) => `ws://example.test${path}`,
    connect: (url) => {
      urls.push(url)
      const socket = new ControlledSocket()
      sockets.push(socket)
      return socket
    },
  }
  const issueTicket: typeof issueChatTicket = async (_body, _owner, signal) => {
    tickets++
    if (stalling) await new Promise<void>((release) => { hung.push({ signal, release }) })
    if (!reachable) throw new TypeError('unreachable')
    return { ticket: `opaque-${tickets}`, session_id: canonicalSession }
  }
  const handlers = { sessionStore: store, transport, issueTicket, random: () => 1, onEvent: (event: ChatInbound) => events.push(event),
    onClose: (code: number) => codes.push(code), onConnectionState: (state: ChatConnectionState) => states.push(state),
    online: network, visible: visibility }
  const connect = () => connectAgentChat('agent-a', 'thread-a', handlers)
  const timed = (extra: Partial<Parameters<typeof connectAgentChat>[2]> = {}) =>
    connectAgentChat('agent-a', 'thread-a', { ...handlers, now: clock.now, schedule: clock.schedule, ...extra })
  const elapse = async (ms: number) => {
    const end = clock.now() + ms
    for (let due = clock.nextDue(); due !== undefined && due <= end; due = clock.nextDue()) {
      clock.advance(Math.max(0, due - clock.now()))
      await drain()
    }
    clock.advance(end - clock.now())
  }
  const socket = () => {
    const latest = sockets.at(-1)
    assert.ok(latest)
    return latest
  }
  return { store, sockets, events, codes, urls, states, network, visibility, clock, handlers, connect, timed, elapse, socket, hung,
    token: () => token, tickets: () => tickets, outage: (down: boolean) => { reachable = !down },
    /** While on, ticket requests never settle by themselves: each is recorded in `hung` until its `release` runs. */
    stall: (on: boolean) => { stalling = on } }
}
