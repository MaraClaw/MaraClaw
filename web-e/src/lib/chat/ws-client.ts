import { z } from 'zod'
import { wsUrl } from '../api.ts'
import { authSession } from '../auth-session.ts'
import { issueChatTicket } from '../auth-api.ts'
import { ApiError } from '../http.ts'
import type { AuthSessionStore, SessionOwner } from '../auth-session.ts'

export type ChatInbound = z.infer<typeof inboundSchema>
export type ChatTurnRejection = z.infer<typeof turnRejectedSchema>

export function isTurnRejected(event: ChatInbound): event is ChatTurnRejection {
  return event.type === 'turn_rejected'
}

export type ChatOutbound = {
  content?: string
  display_content?: string
  file_name?: string
  model_id?: string
  kind?: 'onboarding_trigger'
}

export type ChatSocket = {
  readonly readyState: number
  onopen: (() => void) | null
  onmessage: ((event: { readonly data: unknown }) => void) | null
  onclose: ((event: { readonly code: number }) => void) | null
  send: (data: string) => void
  close: () => void
}

export type ChatTransport = {
  readonly connect: (url: string) => ChatSocket
  readonly url: (path: string) => string
  readonly language: string
}

function connectBrowserSocket(url: string): ChatSocket {
  const socket = new WebSocket(url)
  const connection: ChatSocket = {
    get readyState() { return socket.readyState },
    onopen: null, onmessage: null, onclose: null,
    send: (data) => socket.send(data), close: () => socket.close(),
  }
  socket.onopen = () => connection.onopen?.()
  socket.onmessage = (event) => connection.onmessage?.({ data: event.data })
  socket.onclose = (event) => connection.onclose?.({ code: event.code })
  return connection
}

type Handlers = {
  readonly onEvent: (event: ChatInbound) => void
  readonly onClose?: (code: number) => void
  readonly onReconnect?: () => void
  readonly owner?: SessionOwner
  readonly sessionStore?: AuthSessionStore
  readonly transport?: ChatTransport
  readonly issueTicket?: typeof issueChatTicket
}

const eventSchema = z.object({
  type: z.string(), content: z.string().optional(), session_id: z.string().optional(),
  message_id: z.string().optional(), entry_id: z.string().optional(),
  name: z.string().optional(), status: z.string().optional(), result: z.string().optional(),
  role: z.string().optional(), arguments: z.string().optional(), agent_id: z.string().optional(),
  env: z.string().optional(), output: z.string().optional(),
  live_preview: z.object({
    env: z.string().optional(), screenshot_url: z.string().optional(), output: z.string().optional(),
  }).optional(),
})

const turnRejectedSchema = eventSchema.extend({
  type: z.literal('turn_rejected'),
  reason: z.enum(['quota_exceeded', 'agent_expired', 'no_company_model', 'enqueue_failed']),
  content: z.string(),
})

const inboundSchema = z.union([
  turnRejectedSchema,
  eventSchema.refine((event) => event.type !== 'turn_rejected'),
])

export function connectAgentChat(
  agentId: string,
  sessionId: string | undefined,
  handlers: Handlers,
): { send: (payload: ChatOutbound) => void; close: () => void } {
  const store = handlers.sessionStore ?? authSession
  const owner = handlers.owner ?? store.capture()
  const transport: ChatTransport = handlers.transport ?? {
    connect: connectBrowserSocket, url: wsUrl,
    language: navigator.language || 'en',
  }
  let socket: ChatSocket | null = null
  let stopped = false
  let attempt = 0
  let sequence = 0
  let issuance: AbortController | undefined
  let opened = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const pending: ChatOutbound[] = []

  const current = () => !stopped && store.isCurrent(owner)

  function close() {
    if (stopped) return
    stopped = true
    sequence += 1
    issuance?.abort()
    issuance = undefined
    pending.length = 0
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
    unsubscribe()
    if (socket && socket.readyState < 2) socket.close()
    socket = null
  }

  const unsubscribe = store.subscribe(() => {
    if (!store.isCurrent(owner)) close()
  })

  function disconnected(code: number) {
    if (code === 4001 || code === 4003) {
      close()
      try {
        handlers.onClose?.(code)
      } finally {
        if (code === 4001) store.expire(owner)
      }
      return
    }
    socket = null
    handlers.onClose?.(code)
    if (!current()) return
    const delay = Math.min(8000, 600 * 2 ** attempt)
    attempt += 1
    timer = setTimeout(() => { void open() }, delay)
  }

  async function open() {
    timer = undefined
    if (!current()) { close(); return }
    const attemptId = ++sequence
    issuance?.abort()
    const controller = new AbortController()
    issuance = controller
    const live = () => current() && sequence === attemptId && !controller.signal.aborted
    let source: ChatSocket
    try {
      const ticket = await (handlers.issueTicket ?? issueChatTicket)({
        agent_id: agentId, ...(sessionId === undefined ? {} : { session_id: sessionId }),
      }, owner, controller.signal)
      if (!live()) return
      const params = new URLSearchParams({ ticket: ticket.ticket, session_id: ticket.session_id, lang: transport.language })
      const url = transport.url(`/ws/chat/${agentId}?${params.toString()}`)
      if (!live()) return
      source = transport.connect(url)
    } catch (error) {
      if (!live()) return
      disconnected(error instanceof ApiError && error.status === 401 ? 4001
        : error instanceof ApiError && error.status === 403 ? 4003 : 1011)
      return
    }
    if (!live()) {
      if (source.readyState < 2) source.close()
      return
    }
    socket = source
    const active = () => live() && socket === source
    source.onopen = () => {
      if (!active()) return
      attempt = 0
      if (opened) handlers.onReconnect?.()
      opened = true
      while (active() && pending.length && source.readyState === 1) {
        source.send(JSON.stringify(pending.shift()))
      }
    }
    source.onmessage = (event) => {
      if (!active()) return
      let data: unknown
      try {
        data = JSON.parse(String(event.data))
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error
        handlers.onEvent({ type: 'error', content: 'Malformed chat event' })
        return
      }
      const parsed = inboundSchema.safeParse(data)
      handlers.onEvent(parsed.success ? parsed.data : { type: 'error', content: 'Malformed chat event' })
    }
    source.onclose = (event) => {
      if (!active()) return
      disconnected(event.code)
    }
  }

  void open()

  return {
    send(payload) {
      if (!current()) { close(); return }
      if (socket?.readyState === 1) {
        socket.send(JSON.stringify(payload))
        return
      }
      pending.push(payload)
    },
    close,
  }
}
