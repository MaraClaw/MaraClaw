import { queryOptions } from '@tanstack/react-query'

import type { SessionQueryScope } from '../hooks/session-query-scope.ts'
import { listChatHistoryPage, refreshChatHistory, type ChatHistoryTarget } from './chat-history-api.ts'
import { applyHistoryRefresh, prependHistory, type ChatHistory } from './chat/history.ts'

export const chatHistoryKey = (target: ChatHistoryTarget) => ['chat-history', target.agentId, target.sessionId] as const
const pageKey = (target: ChatHistoryTarget) => ['chat-history-page', target.agentId, target.sessionId] as const

export class ChatHistoryCursorError extends Error {
  constructor() { super('The conversation cursor did not advance. Try refreshing the conversation.'); this.name = 'ChatHistoryCursorError' }
}

export function chatLatestOptions(scope: SessionQueryScope, target: ChatHistoryTarget) {
  return queryOptions({
    queryKey: ['messages', target.agentId, target.sessionId],
    queryFn: ({ signal }) => scope.run(async () => {
      const held = scope.client.getQueryData<ChatHistory>(chatHistoryKey(target))
      const boundary = held?.items.at(-1)
      const request = { owner: scope.owner, signal }
      const refresh = held?.revision && boundary?.created_at && boundary.message_id
        ? await refreshChatHistory(target, { revision: held.revision, created_at: boundary.created_at, message_id: boundary.message_id }, request)
        : { kind: 'reset', page: await listChatHistoryPage(target, request) } as const
      signal.throwIfAborted()
      scope.assertCurrent()
      scope.client.setQueryData<ChatHistory>(chatHistoryKey(target), (current) => {
        if (current) return applyHistoryRefresh(current, refresh)
        return refresh.kind === 'reset' ? refresh.page : current
      })
      return refresh
    }),
    staleTime: 0,
    refetchOnMount: 'always',
    retry: false,
  })
}

export async function loadEarlierHistory(scope: SessionQueryScope, target: ChatHistoryTarget, signal: AbortSignal) {
  return scope.run(async () => {
    signal.throwIfAborted()
    const current = scope.client.getQueryData<ChatHistory>(chatHistoryKey(target))
    const cursor = current?.next_cursor
    if (!current || cursor == null) return
    const page = await scope.client.fetchQuery({
      queryKey: [...pageKey(target), current.revision, cursor],
      queryFn: ({ signal: querySignal }) => scope.run(() => listChatHistoryPage(target, {
        cursor, owner: scope.owner, signal: AbortSignal.any([signal, querySignal]),
      })),
      staleTime: Infinity,
      retry: false,
    })
    if (page.next_cursor === cursor) throw new ChatHistoryCursorError()
    signal.throwIfAborted()
    scope.assertCurrent()
    scope.client.setQueryData<ChatHistory>(chatHistoryKey(target), (latest) =>
      latest?.next_cursor === cursor ? prependHistory(latest, page) : latest)
  })
}
