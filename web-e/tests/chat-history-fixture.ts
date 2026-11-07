import type { ChatHistoryPage } from '../src/lib/chat-history-api.ts'

export const historyTarget = { agentId: 'chat-agent', sessionId: 'chat-session' }
export const historyCursor = 'older/+opaque?=& boundary'
export const historyRow = (index: number) => ({ entry_id: `entry-${index}`, message_id: `00000000-0000-0000-0000-${index.toString(16).padStart(12, '0')}`,
  created_at: new Date(Date.UTC(2026, 9, 24) + index * 1000).toISOString(), role: 'assistant', content: `Message ${index}` })
export const historyRows = (start: number, end: number) => Array.from({ length: end - start }, (_, index) => historyRow(start + index))
export const historyPage = (start: number, end: number, cursor: string | null = null): ChatHistoryPage => ({
  items: historyRows(start, end), next_cursor: cursor, has_more: cursor !== null, revision: `revision-${end}`,
})
