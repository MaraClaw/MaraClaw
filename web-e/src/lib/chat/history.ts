import type { ChatHistoryPage, ChatHistoryRefresh } from '../chat-history-api.ts'

export type HistoryEntry = ChatHistoryPage['items'][number]
export type ChatHistory = {
  readonly items: readonly HistoryEntry[]
  readonly next_cursor: string | null
  readonly has_more: boolean
  readonly revision: string | null
}

// Both ranges are chronological. Prefer the newer range at an entry overlap,
// never at a parent overlap: a parent's projected siblings can span two pages.
export function joinHistoryRanges(earlier: readonly HistoryEntry[], later: readonly HistoryEntry[]): readonly HistoryEntry[] {
  const entries = new Map(later.map((row) => [row.entry_id, row]))
  const prefix = new Map(earlier.filter((row) => !entries.has(row.entry_id)).map((row) => [row.entry_id, row]))
  return [...prefix.values(), ...entries.values()].sort((left, right) => {
    if (!left.created_at || !right.created_at) return 0
    const milliseconds = Date.parse(left.created_at) - Date.parse(right.created_at)
    if (milliseconds !== 0) return milliseconds
    // Date truncates PostgreSQL microseconds. Preserve their tie ordering.
    const leftFraction = /\.(\d+)/.exec(left.created_at)?.[1]?.padEnd(6, '0') ?? '000000'
    const rightFraction = /\.(\d+)/.exec(right.created_at)?.[1]?.padEnd(6, '0') ?? '000000'
    return leftFraction.localeCompare(rightFraction) || (left.message_id ?? '').localeCompare(right.message_id ?? '')
  })
}

export function refreshHistory(current: ChatHistory | undefined, range: ChatHistoryPage): ChatHistory {
  if (!current) return { ...range, items: joinHistoryRanges([], range.items) }
  return { ...current, revision: range.revision, items: joinHistoryRanges(current.items, range.items) }
}

export function applyHistoryRefresh(current: ChatHistory, refresh: ChatHistoryRefresh): ChatHistory {
  switch (refresh.kind) {
    case 'unchanged':
      return current
    case 'reset':
      return refresh.page
    case 'delta': {
      // A delta contains complete message projections. Remove the old siblings
      // too if an edited message now projects into fewer entries.
      const parents = new Set(refresh.items.map((row) => row.message_id))
      const retained = current.items.filter((row) => row.message_id === undefined || !parents.has(row.message_id))
      const items = joinHistoryRanges(retained, refresh.items)
      return { ...current, revision: refresh.revision, items }
    }
    default: {
      const unreachable: never = refresh
      return unreachable
    }
  }
}

export function prependHistory(current: ChatHistory, page: ChatHistoryPage): ChatHistory {
  return { ...page, revision: current.revision, items: joinHistoryRanges(page.items, current.items) }
}
