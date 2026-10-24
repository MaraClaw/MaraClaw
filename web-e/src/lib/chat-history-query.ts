import { queryOptions } from '@tanstack/react-query'

import type { SessionQueryScope } from '../hooks/session-query-scope.ts'
import { listChatHistoryPage, type ChatHistoryTarget } from './chat-history-api.ts'
import { joinHistoryRanges, prependHistory, refreshHistory, type ChatHistory } from './chat/history.ts'

export const chatHistoryKey = (target: ChatHistoryTarget) => ['chat-history', target.agentId, target.sessionId] as const
const pageKey = (target: ChatHistoryTarget) => ['chat-history-page', target.agentId, target.sessionId] as const

export class ChatHistoryCursorError extends Error {
  constructor() { super('The conversation cursor did not advance. Try refreshing the conversation.'); this.name = 'ChatHistoryCursorError' }
}

export function chatLatestOptions(scope: SessionQueryScope, target: ChatHistoryTarget) {
  return queryOptions({
    queryKey: ['messages', target.agentId, target.sessionId],
    queryFn: ({ signal }) => scope.run(async () => {
      const boundary = scope.client.getQueryData<ChatHistory>(chatHistoryKey(target))?.items.at(-1)?.entry_id
      let range = await listChatHistoryPage(target, { owner: scope.owner, signal })
      const cursors = new Set<string>()
      // A latest window can miss more than 50 new entries. Bridge all the way to
      // the previous newest entry before publishing, so no intervening row is lost.
      while (boundary && !range.items.some((row) => row.entry_id === boundary) && range.next_cursor !== null) {
        if (cursors.has(range.next_cursor)) throw new ChatHistoryCursorError()
        cursors.add(range.next_cursor)
        const older = await listChatHistoryPage(target, { cursor: range.next_cursor, owner: scope.owner, signal })
        range = { ...older, items: joinHistoryRanges(older.items, range.items) }
      }
      signal.throwIfAborted()
      scope.assertCurrent()
      scope.client.setQueryData<ChatHistory>(chatHistoryKey(target), (current) => refreshHistory(current, range))
      return range
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
      queryKey: [...pageKey(target), cursor],
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
