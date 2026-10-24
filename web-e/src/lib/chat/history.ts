import type { ChatHistoryPage } from '../chat-history-api.ts'

export type HistoryEntry = ChatHistoryPage['items'][number]
export type ChatHistory = {
  readonly items: readonly HistoryEntry[]
  readonly next_cursor: string | null
  readonly has_more: boolean
}

// Both ranges are chronological. Prefer the newer range at an entry overlap,
// never at a parent overlap: a parent's projected siblings can span two pages.
export function joinHistoryRanges(earlier: readonly HistoryEntry[], later: readonly HistoryEntry[]): readonly HistoryEntry[] {
  const entries = new Map(later.map((row) => [row.entry_id, row]))
  const prefix = new Map(earlier.filter((row) => !entries.has(row.entry_id)).map((row) => [row.entry_id, row]))
  return [...prefix.values(), ...entries.values()]
}

export function refreshHistory(current: ChatHistory | undefined, range: ChatHistoryPage): ChatHistory {
  if (!current) return { ...range, items: joinHistoryRanges([], range.items) }
  return { ...current, items: joinHistoryRanges(current.items, range.items) }
}

export function prependHistory(current: ChatHistory, page: ChatHistoryPage): ChatHistory {
  return { ...page, items: joinHistoryRanges(page.items, current.items) }
}
