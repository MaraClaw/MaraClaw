import { expect, type Page, type WebSocketRoute } from '@playwright/test'
import { z } from 'zod'
import type { ChatMessage } from '../src/lib/chat/message'
import { originalAgent, originalToken, originalUser, prepareWorkspace, replacementAgent, replacementToken } from './session-fencing-fixture'

export const firstSession = '387b65d4-6a8d-42f4-9677-12d4caaf6d31'
export const secondSession = 'c47bd538-b299-41eb-b6eb-cf0c28f6bc46'
export const archivedSession = '647085d3-c393-487c-9c9f-021a493ed319'
export const firstMessage = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
export const secondMessage = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
export const chatPath = (session = firstSession, agent = originalAgent) => `/app/agents/${agent.id}/chat/${session}`
export const message = (message_id: string, content: string, role = 'assistant'): ChatMessage => ({
  message_id, entry_id: message_id, role, content, created_at: '2030-01-01T00:00:00Z',
})

export async function navigateChat(page: Page, path: string) {
  // Exercise BrowserRouter in the same document now that chat has no session picker.
  const marker = await page.evaluate((path) => {
    const marker = crypto.randomUUID()
    document.documentElement.dataset.chatNavigation = marker
    const state = { ...window.history.state, key: marker, idx: (window.history.state?.idx ?? 0) + 1 }
    window.history.pushState(state, '', path)
    window.dispatchEvent(new PopStateEvent('popstate', { state }))
    return marker
  }, path)
  await expect(page).toHaveURL(path)
  expect(await page.evaluate(() => document.documentElement.dataset.chatNavigation)).toBe(marker)
}

export async function prepareChat(page: Page, initial: readonly ChatMessage[] = []) {
  await prepareWorkspace(page)
  let history = initial
  let onboarded = originalAgent.onboarded_for_me
  let requests = 0
  let ensures = 0
  let deferred: { readonly release: ReturnType<typeof Promise.withResolvers<void>>; readonly finished: ReturnType<typeof Promise.withResolvers<void>> } | undefined
  let delayedTicket: { readonly requested: ReturnType<typeof Promise.withResolvers<void>>; readonly response: ReturnType<typeof Promise.withResolvers<number>>; readonly finished: ReturnType<typeof Promise.withResolvers<void>> } | undefined
  const tickets: { readonly ticket: string; readonly session_id: string; readonly agent_id: string }[] = []
  const used = new Set<string>()
  const sockets: { readonly route: WebSocketRoute; readonly session: string | null; readonly ticket: string | null; readonly sent: string[] }[] = []
  await page.route('**/api/auth/chat-ticket', async (route) => {
    expect(route.request().method()).toBe('POST')
    const body = z.object({ agent_id: z.string(), session_id: z.string().optional() }).parse(route.request().postDataJSON())
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(route.request().headers().authorization)
    const issued = { ticket: `opaque-chat-${tickets.length + 1}`, session_id: body.session_id ?? firstSession, agent_id: body.agent_id }
    tickets.push(issued)
    const gate = delayedTicket
    delayedTicket = undefined
    gate?.requested.resolve()
    const status = gate ? await gate.response.promise : 200
    await route.fulfill({ status, json: status === 200 ? { ticket: issued.ticket, session_id: issued.session_id } : { detail: 'Retired ticket denied' } })
    gate?.finished.resolve()
  })
  await page.route('**/api/agents/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const agent = path.includes(replacementAgent.id) ? replacementAgent : originalAgent
    if (path === `/api/agents/${agent.id}`) return route.fulfill({ json: { ...agent, onboarded_for_me: onboarded } })
    if (path.endsWith('/metrics')) return route.fulfill({ json: { status: agent.status, tasks: { total: 0, done: 0, pending: 0 } } })
    const shared = {
      id: firstSession, agent_id: agent.id, user_id: 'canonical-creator-not-viewer', title: 'Identity thread 1',
      source_channel: 'shared', is_primary: true, created_at: '2029-01-01T00:00:00Z', message_count: history.length, unread_count: 0,
    }
    if (path.endsWith('/sessions') && route.request().method() === 'POST') {
      ensures++
      return route.fulfill({ json: shared })
    }
    if (path.endsWith('/sessions')) return route.fulfill({ json: [{
      id: archivedSession, agent_id: agent.id, user_id: originalUser.id, title: 'Archived private thread',
      source_channel: 'web', is_primary: true, created_at: '2031-01-01T00:00:00Z', message_count: 1, unread_count: 0,
    }, shared, {
      id: secondSession, agent_id: agent.id, user_id: originalUser.id, title: 'Identity thread 2',
      source_channel: 'agent', created_at: '2030-01-01T00:00:00Z', message_count: 1, unread_count: 0,
    }] })
    if (path.endsWith('/messages/page')) {
      const params = new URL(route.request().url()).searchParams
      expect(route.request().method()).toBe('GET')
      expect(params.get('limit')).toBe('50')
      requests++
      const snapshot = path.includes(firstSession) ? history : path.includes(archivedSession)
        ? [message('archive-reply', 'Authorized archived history')] : [message('other-session', 'Current second session')]
      const cursor = params.get('cursor')
      const end = cursor === null ? snapshot.length : snapshot.findIndex((row) => row.entry_id === cursor)
      expect(end).toBeGreaterThanOrEqual(0)
      const start = Math.max(0, end - 50)
      const gate = deferred
      deferred = undefined
      if (gate) await gate.release.promise
      try {
        return await route.fulfill({ json: { items: snapshot.slice(start, end), next_cursor: start > 0 ? snapshot[start]?.entry_id : null, has_more: start > 0 } })
      } finally {
        gate?.finished.resolve()
      }
    }
    if (path.endsWith('/files/') && route.request().method() === 'GET') {
      expect(new URL(route.request().url()).searchParams.get('path')).toBe('skills')
      return route.fulfill({ status: 404, json: { detail: 'No installed skills directory' } })
    }
    return route.fallback()
  })
  await page.routeWebSocket('**/ws/chat/**', (route) => {
    const url = new URL(route.url())
    const issued = tickets.find((item) => item.ticket === url.searchParams.get('ticket'))
    expect(issued).toBeDefined()
    expect([...url.searchParams.keys()].sort()).toEqual(['lang', 'session_id', 'ticket'])
    expect(url.searchParams.get('session_id')).toBe(issued?.session_id)
    expect(url.pathname).toBe(`/ws/chat/${issued?.agent_id}`)
    expect(used.has(issued?.ticket ?? '')).toBe(false)
    used.add(issued?.ticket ?? '')
    const sent: string[] = []
    sockets.push({ route, session: url.searchParams.get('session_id'), ticket: url.searchParams.get('ticket'), sent })
    route.onMessage((data) => sent.push(String(data)))
    route.send(JSON.stringify({ type: 'connected', session_id: url.searchParams.get('session_id') }))
  })
  return {
    sockets, tickets, requests: () => requests, ensures: () => ensures,
    holdNextTicket: () => {
      const gate = { requested: Promise.withResolvers<void>(), response: Promise.withResolvers<number>(), finished: Promise.withResolvers<void>() }
      delayedTicket = gate
      return { requested: gate.requested.promise, release: async (status = 200) => { gate.response.resolve(status); await gate.finished.promise } }
    },
    setHistory: (rows: readonly ChatMessage[]) => { history = rows },
    setOnboarded: (value: boolean) => { onboarded = value },
    holdNextHistory: () => {
      const gate = { release: Promise.withResolvers<void>(), finished: Promise.withResolvers<void>() }
      deferred = gate
      return async () => { gate.release.resolve(); await gate.finished.promise }
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

export function submitted(socket: { readonly sent: readonly string[] }, index = 0) {
  return z.object({ client_submission_id: z.uuid(), content: z.string(), display_content: z.string().optional(),
    file_name: z.string().optional(), model_id: z.string().optional() }).parse(JSON.parse(socket.sent[index] ?? 'null'))
}

export function accept(socket: { readonly route: WebSocketRoute; readonly session: string | null }, client_submission_id: string, message_id = firstMessage) {
  socket.route.send(JSON.stringify({ type: 'turn_accepted', client_submission_id, message_id, session_id: socket.session }))
}
