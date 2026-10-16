import { expect, type Page, type WebSocketRoute } from '@playwright/test'
import type { ChatMessage } from '../src/lib/chat/message'
import { originalAgent, originalUser, prepareWorkspace, replacementAgent } from './session-fencing-fixture'

export const firstSession = '387b65d4-6a8d-42f4-9677-12d4caaf6d31'
export const secondSession = 'c47bd538-b299-41eb-b6eb-cf0c28f6bc46'
export const chatPath = (session = firstSession, agent = originalAgent) => `/app/agents/${agent.id}/chat/${session}`
export const message = (message_id: string, content: string, role = 'assistant'): ChatMessage => ({
  message_id, entry_id: message_id, role, content, created_at: '2030-01-01T00:00:00Z',
})

export async function prepareChat(page: Page, initial: readonly ChatMessage[] = []) {
  await prepareWorkspace(page)
  let history = initial
  let requests = 0
  let deferred: ReturnType<typeof Promise.withResolvers<void>> | undefined
  const sockets: { readonly route: WebSocketRoute; readonly session: string | null; readonly sent: string[] }[] = []
  await page.route('**/api/agents/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const agent = path.includes(replacementAgent.id) ? replacementAgent : originalAgent
    if (path === `/api/agents/${agent.id}`) return route.fulfill({ json: agent })
    if (path.endsWith('/metrics')) return route.fulfill({ json: { status: agent.status, tasks: { total: 0, done: 0, pending: 0 } } })
    if (path.endsWith('/sessions')) return route.fulfill({ json: [firstSession, secondSession].map((id, index) => ({
      id, agent_id: agent.id, user_id: originalUser.id, title: `Identity thread ${index + 1}`,
      source_channel: 'web', created_at: '2030-01-01T00:00:00Z', message_count: 0, unread_count: 0,
    })) })
    if (path.endsWith('/messages')) {
      requests++
      const snapshot = path.includes(firstSession) ? history : [message('other-session', 'Current second session')]
      const gate = deferred
      deferred = undefined
      if (gate) await gate.promise
      return route.fulfill({ json: snapshot })
    }
    return route.fallback()
  })
  await page.routeWebSocket('**/ws/chat/**', (route) => {
    const sent: string[] = []
    sockets.push({ route, session: new URL(route.url()).searchParams.get('session_id'), sent })
    route.onMessage((data) => sent.push(String(data)))
    route.send(JSON.stringify({ type: 'connected' }))
  })
  return {
    sockets, requests: () => requests,
    setHistory: (rows: readonly ChatMessage[]) => { history = rows },
    holdNextHistory: () => {
      const gate = Promise.withResolvers<void>()
      deferred = gate
      return () => gate.resolve()
    },
    socket: async (index = 0) => {
      await expect.poll(() => sockets.length).toBeGreaterThan(index)
      const socket = sockets[index]
      if (!socket) throw new Error('Expected routed chat socket')
      return socket
    },
  }
}

export function complete(socket: WebSocketRoute, row: ChatMessage) {
  socket.send(JSON.stringify({ type: 'done', ...row }))
}
