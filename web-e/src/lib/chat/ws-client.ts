import { wsUrl } from '@/lib/api'

export type ChatInbound = {
  type: string
  content?: string
  session_id?: string
  name?: string
  status?: string
  result?: string
  role?: string
  arguments?: string
  agent_id?: string
  env?: string
  output?: string
  live_preview?: { env?: string; screenshot_url?: string; output?: string }
}

export type ChatOutbound = {
  content?: string
  display_content?: string
  file_name?: string
  model_id?: string
  kind?: 'onboarding_trigger'
}

type Handlers = {
  onEvent: (event: ChatInbound) => void
  onClose?: (code: number) => void
}

export function connectAgentChat(
  agentId: string,
  token: string,
  sessionId: string | undefined,
  handlers: Handlers,
): { send: (payload: ChatOutbound) => void; close: () => void } {
  let socket: WebSocket | null = null
  let stopped = false
  let attempt = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  const pending: ChatOutbound[] = []

  const params = new URLSearchParams({ token, lang: navigator.language || 'en' })
  if (sessionId) params.set('session_id', sessionId)
  const url = wsUrl(`/ws/chat/${agentId}?${params.toString()}`)

  function open() {
    socket = new WebSocket(url)
    socket.addEventListener('open', () => {
      attempt = 0
      while (pending.length && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(pending.shift()))
      }
    })
    socket.addEventListener('message', (event) => {
      try {
        handlers.onEvent(JSON.parse(String(event.data)) as ChatInbound)
      } catch {
        handlers.onEvent({ type: 'error', content: 'Malformed chat event' })
      }
    })
    socket.addEventListener('close', (event) => {
      handlers.onClose?.(event.code)
      if (stopped || event.code === 4001 || event.code === 4003) return
      const delay = Math.min(8000, 600 * 2 ** attempt)
      attempt += 1
      timer = setTimeout(open, delay)
    })
  }

  open()

  return {
    send(payload) {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload))
        return
      }
      pending.push(payload)
    },
    close() {
      stopped = true
      if (timer) clearTimeout(timer)
      socket?.close()
    },
  }
}
