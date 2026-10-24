import type { ChatHistoryPage } from '../src/lib/chat-history-api.ts'

export const historyTarget = { agentId: 'chat-agent', sessionId: 'chat-session' }
export const historyCursor = 'older/+opaque?=& boundary'
export const historyRow = (index: number) => ({ entry_id: `entry-${index}`, message_id: `turn-${index}`, role: 'assistant', content: `Message ${index}` })
export const historyRows = (start: number, end: number) => Array.from({ length: end - start }, (_, index) => historyRow(start + index))
export const historyPage = (start: number, end: number, cursor: string | null = null): ChatHistoryPage => ({
  items: historyRows(start, end), next_cursor: cursor, has_more: cursor !== null,
})
