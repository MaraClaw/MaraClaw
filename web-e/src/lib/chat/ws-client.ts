import { z } from 'zod'
import { wsUrl } from '../api.ts'
import { authSession } from '../auth-session.ts'
import type { AuthSessionStore, SessionOwner } from '../auth-session.ts'

export type ChatInbound = z.infer<typeof inboundSchema>

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
  readonly owner?: SessionOwner
  readonly sessionStore?: AuthSessionStore
  readonly transport?: ChatTransport
}

const inboundSchema = z.object({
  type: z.string(), content: z.string().optional(), session_id: z.string().optional(),
  name: z.string().optional(), status: z.string().optional(), result: z.string().optional(),
  role: z.string().optional(), arguments: z.string().optional(), agent_id: z.string().optional(),
  env: z.string().optional(), output: z.string().optional(),
  live_preview: z.object({
    env: z.string().optional(), screenshot_url: z.string().optional(), output: z.string().optional(),
  }).optional(),
})

export function connectAgentChat(
  agentId: string,
  token: string,
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
  let timer: ReturnType<typeof setTimeout> | undefined
  const pending: ChatOutbound[] = []

  const current = () => !stopped && store.isCurrent(owner)
  const params = new URLSearchParams({ token, lang: transport.language })
  if (sessionId) params.set('session_id', sessionId)

  function close() {
    if (stopped) return
    stopped = true
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

  function open() {
    timer = undefined
    if (!current()) { close(); return }
    const source = transport.connect(transport.url(`/ws/chat/${agentId}?${params.toString()}`))
    if (!current()) {
      if (source.readyState < 2) source.close()
      close()
      return
    }
    socket = source
    const active = () => current() && socket === source
    source.onopen = () => {
      if (!active()) return
      attempt = 0
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
      if (event.code === 4001 || event.code === 4003) {
        close()
        try {
          handlers.onClose?.(event.code)
        } finally {
          if (event.code === 4001) store.expire(owner)
        }
        return
      }
      socket = null
      handlers.onClose?.(event.code)
      if (!current()) return
      const delay = Math.min(8000, 600 * 2 ** attempt)
      attempt += 1
      timer = setTimeout(open, delay)
    }
  }

  open()

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
