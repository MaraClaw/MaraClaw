import { wsUrl } from '../api.ts'
import { authSession } from '../auth-session.ts'
import { issueChatTicket } from '../auth-api.ts'
import { ApiError } from '../http.ts'
import type { AuthSessionStore, SessionOwner } from '../auth-session.ts'
import { ChatEventType, inboundSchema, isTurnAccepted, isTurnRejected, type ChatInbound, type ChatOutbound } from './protocol.ts'
import { createChatSubmissions, type ChatSubmissions } from './submissions.ts'
export { isTurnAccepted, isTurnRejected } from './protocol.ts'
export type { ChatInbound, ChatOutbound, ChatTurnAcceptance, ChatTurnRejection } from './protocol.ts'

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

const BACKOFF_BASE_MS = 600
const BACKOFF_CAP_MS = 8000
const LIVENESS_DEADLINE_MS = 25_000
const CONNECT_DEADLINE_MS = 15_000
// A deadline timer this late woke a throttled or sleeping tab; frames queued meanwhile may not be dispatched yet.
const LIVENESS_GRACE_MS = 1000
const RETRY_WINDOW_MS = 120_000

export type ChatSignal = {
  readonly current: () => boolean
  readonly subscribe: (listener: () => void) => () => void
}

function browserSchedule(callback: () => void, delay: number): () => void {
  const id = setTimeout(callback, delay)
  return () => clearTimeout(id)
}

const browserOnline: ChatSignal = {
  current: () => navigator.onLine,
  subscribe(listener) {
    window.addEventListener('online', listener)
    window.addEventListener('offline', listener)
    return () => {
      window.removeEventListener('online', listener)
      window.removeEventListener('offline', listener)
    }
  },
}

const browserVisible: ChatSignal = {
  current: () => document.visibilityState === 'visible',
  subscribe(listener) {
    document.addEventListener('visibilitychange', listener)
    return () => document.removeEventListener('visibilitychange', listener)
  },
}

// Half to all of the capped exponential delay, so clients dropped together do not reconnect in lockstep.
function backoffDelay(attempt: number, random: () => number): number {
  return Math.round(Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt) * (0.5 + 0.5 * random()))
}

export type ChatConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'suspended'

type Handlers = {
  readonly onEvent: (event: ChatInbound) => void
  readonly onClose?: (code: number) => void
  readonly onReconnect?: () => void
  readonly onConnectionState?: (state: ChatConnectionState) => void
  readonly owner?: SessionOwner
  readonly sessionStore?: AuthSessionStore
  readonly transport?: ChatTransport
  readonly issueTicket?: typeof issueChatTicket
  readonly submissions?: ChatSubmissions
  readonly random?: () => number
  readonly now?: () => number
  readonly schedule?: (callback: () => void, delay: number) => () => void
  readonly online?: ChatSignal
  readonly visible?: ChatSignal
}

export function connectAgentChat(
  agentId: string,
  sessionId: string | undefined,
  handlers: Handlers,
): { send: (payload: ChatOutbound) => void; close: () => void; retry: () => void } {
  const store = handlers.sessionStore ?? authSession
  const owner = handlers.owner ?? store.capture()
  const submissions = handlers.submissions ?? createChatSubmissions(owner, agentId, sessionId)
  const transport: ChatTransport = handlers.transport ?? {
    connect: connectBrowserSocket, url: wsUrl,
    language: navigator.language || 'en',
  }
  const now = handlers.now ?? Date.now
  const schedule = handlers.schedule ?? browserSchedule
  const online = handlers.online ?? browserOnline
  const visible = handlers.visible ?? browserVisible
  let socket: ChatSocket | null = null
  let stopped = false
  let attempt = 0
  let sequence = 0
  let issuance: AbortController | undefined
  let opened = false
  let tried = false
  let timer: (() => void) | undefined
  let liveness: (() => void) | undefined
  let connectDeadline: (() => void) | undefined
  let lastFrameAt = 0
  let healthyAt = now()
  let state: ChatConnectionState | undefined
  const pending: ChatOutbound[] = []

  const current = () => !stopped && store.isCurrent(owner) && submissions.matches(owner, agentId, sessionId)

  function setState(next: ChatConnectionState) {
    if (state === next) return
    state = next
    handlers.onConnectionState?.(next)
  }

  // Drops the attempt, timers and socket; the ledger, queued triggers and subscriptions stay.
  function halt() {
    sequence += 1
    issuance?.abort()
    issuance = undefined
    timer?.()
    timer = undefined
    liveness?.()
    liveness = undefined
    connectDeadline?.()
    connectDeadline = undefined
    if (!socket) return
    socket.onopen = socket.onmessage = socket.onclose = null
    if (socket.readyState < 2) socket.close()
    socket = null
  }

  function close() {
    if (stopped) return
    stopped = true
    halt()
    pending.length = 0
    unsubscribe()
    unwatchNetwork()
    unwatchVisibility()
  }

  const unsubscribe = store.subscribe(() => {
    if (!store.isCurrent(owner)) { submissions.retire(); close() }
  })

  function goOffline() {
    halt()
    setState('offline')
  }

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
    halt()
    handlers.onClose?.(code)
    retryLater()
  }

  function retryLater() {
    if (!current()) return
    if (!online.current()) { goOffline(); return }
    if (state === 'connected') healthyAt = now()
    const remaining = RETRY_WINDOW_MS - (now() - healthyAt)
    if (remaining <= 0) { setState('suspended'); return }
    setState('reconnecting')
    const delay = Math.min(backoffDelay(attempt, handlers.random ?? Math.random), remaining)
    attempt += 1
    timer = schedule(() => { void open() }, delay)
  }

  // Decides from the last-frame time, never from how many times the timer ran.
  function watch(delay: number, graced: boolean) {
    const due = now() + delay
    liveness = schedule(() => {
      liveness = undefined
      const quiet = now() - lastFrameAt
      if (quiet < LIVENESS_DEADLINE_MS) watch(LIVENESS_DEADLINE_MS - quiet, false)
      else if (!graced && now() - due > LIVENESS_GRACE_MS) watch(LIVENESS_GRACE_MS, true)
      else { halt(); retryLater() }
    }, delay)
  }

  // Single flight: no-op while an attempt is in flight or a socket is live.
  function resume() {
    if (!current()) { close(); return }
    if (issuance || socket) return
    if (!online.current()) { goOffline(); return }
    if (state === 'suspended' || state === 'offline') healthyAt = now()
    timer?.()
    void open()
  }

  const unwatchNetwork = online.subscribe(() => { if (online.current()) resume(); else goOffline() })
  const unwatchVisibility = visible.subscribe(() => { if (visible.current()) resume() })

  async function open() {
    timer = undefined
    if (!current()) { close(); return }
    if (!online.current()) { goOffline(); return }
    if (now() - healthyAt >= RETRY_WINDOW_MS) { setState('suspended'); return }
    setState(tried ? 'reconnecting' : 'connecting')
    tried = true
    const attemptId = ++sequence
    issuance?.abort()
    const controller = new AbortController()
    issuance = controller
    connectDeadline = schedule(() => {
      connectDeadline = undefined
      halt()
      retryLater()
    }, Math.min(CONNECT_DEADLINE_MS, RETRY_WINDOW_MS - (now() - healthyAt)))
    const live = () => current() && sequence === attemptId && !controller.signal.aborted
    let source: ChatSocket
    try {
      const ticket = await (handlers.issueTicket ?? issueChatTicket)({
        agent_id: agentId, ...(submissions.sessionId === undefined ? {} : { session_id: submissions.sessionId }),
      }, owner, controller.signal)
      if (!live()) return
      issuance = undefined
      submissions.resolve(ticket.session_id)
      const params = new URLSearchParams({ ticket: ticket.ticket, session_id: ticket.session_id, lang: transport.language, heartbeat: '1' })
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
      if (opened) handlers.onReconnect?.()
      opened = true
      for (const payload of submissions.pending()) {
        if (!active() || source.readyState !== 1) break
        source.send(JSON.stringify(payload))
      }
      while (active() && pending.length && source.readyState === 1) {
        source.send(JSON.stringify(pending.shift()))
      }
    }
    source.onmessage = (event) => {
      if (!active()) return
      lastFrameAt = now()
      let data: unknown
      try {
        data = JSON.parse(String(event.data))
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error
        handlers.onEvent({ type: ChatEventType.ERROR, content: 'Malformed chat event' })
        return
      }
      const parsed = inboundSchema.safeParse(data)
      if (!parsed.success) { handlers.onEvent({ type: ChatEventType.ERROR, content: 'Malformed chat event' }); return }
      if (parsed.data.type === ChatEventType.CONNECTED) {
        attempt = 0
        setState('connected')
        connectDeadline?.()
        connectDeadline = undefined
        liveness?.()
        watch(LIVENESS_DEADLINE_MS, false)
      }
      if (parsed.data.type === ChatEventType.HEARTBEAT) return
      if (isTurnAccepted(parsed.data) && !submissions.accept(parsed.data)) return
          if (isTurnRejected(parsed.data) && 'client_submission_id' in parsed.data) submissions.reject(parsed.data.client_submission_id)
      handlers.onEvent(parsed.data)
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
      const envelope = payload.kind === 'onboarding_trigger' ? payload : submissions.submit(payload)
      if (!envelope) return
      if (socket?.readyState === 1) {
        socket.send(JSON.stringify(envelope))
        return
      }
      if (envelope.kind === 'onboarding_trigger') pending.push(envelope)
      if (state === 'suspended') resume()
    },
    close,
    retry: resume,
  }
}
