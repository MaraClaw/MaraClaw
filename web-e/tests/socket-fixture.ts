import assert from 'node:assert/strict'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { connectAgentChat } from '../src/lib/chat/ws-client.ts'
import type { issueChatTicket } from '../src/lib/auth-api.ts'
import type { ChatInbound, ChatSocket, ChatTransport } from '../src/lib/chat/ws-client.ts'
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

export function fixture() {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({
    read: () => token, write: (value) => { token = value }, clear: () => { token = null },
  })
  const sockets: ControlledSocket[] = []
  const events: ChatInbound[] = []
  const codes: number[] = []
  const urls: string[] = []
  let tickets = 0
  const transport: ChatTransport = {
    language: 'en', url: (path) => `ws://example.test${path}`,
    connect: (url) => {
      urls.push(url)
      const socket = new ControlledSocket()
      sockets.push(socket)
      return socket
    },
  }
  const issueTicket: typeof issueChatTicket = async () => ({ ticket: `opaque-${++tickets}`, session_id: canonicalSession })
  const handlers = { sessionStore: store, transport, issueTicket, onEvent: (event: ChatInbound) => events.push(event),
    onClose: (code: number) => codes.push(code) }
  const connect = () => connectAgentChat('agent-a', 'thread-a', handlers)
  const socket = () => {
    const latest = sockets.at(-1)
    assert.ok(latest)
    return latest
  }
  return { store, sockets, events, codes, urls, handlers, connect, socket, token: () => token }
}
