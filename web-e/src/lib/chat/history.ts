import type { ChatHistoryPage, ChatHistoryRefresh } from '../chat-history-api.ts'

export type HistoryEntry = ChatHistoryPage['items'][number]
export type ChatHistory = {
  readonly items: readonly HistoryEntry[]
  readonly next_cursor: string | null
  readonly has_more: boolean
  readonly revision: string | null
  readonly pages?: readonly HistoryPageBoundary[]
  readonly floor?: HistoryPosition
}

export type HistoryPosition = {
  readonly created_at?: HistoryEntry['created_at']
  readonly message_id?: HistoryEntry['message_id']
}
type HistoryPageBoundary = {
  readonly first: HistoryPosition & { readonly entry_id: string }
  readonly next_cursor: string | null
}
export const RETAINED_HISTORY_LIMIT = 500

export function compareHistoryPosition(left: HistoryPosition, right: HistoryPosition): number {
  if (!left.created_at || !right.created_at) return 0
  const milliseconds = Date.parse(left.created_at) - Date.parse(right.created_at)
  if (milliseconds !== 0) return milliseconds
  const leftFraction = /\.(\d+)/.exec(left.created_at)?.[1]?.padEnd(6, '0') ?? '000000'
  const rightFraction = /\.(\d+)/.exec(right.created_at)?.[1]?.padEnd(6, '0') ?? '000000'
  return leftFraction.localeCompare(rightFraction) || (left.message_id ?? '').localeCompare(right.message_id ?? '')
}

function pageBoundaries(page: ChatHistory): readonly HistoryPageBoundary[] {
  const first = page.items[0]
  return page.pages ?? (first ? [{ first: { entry_id: first.entry_id, created_at: first.created_at, message_id: first.message_id }, next_cursor: page.next_cursor }] : [])
}

// Both ranges are chronological. Prefer the newer range at an entry overlap,
// never at a parent overlap: a parent's projected siblings can span two pages.
export function joinHistoryRanges(earlier: readonly HistoryEntry[], later: readonly HistoryEntry[]): readonly HistoryEntry[] {
  if (later.length === 0) return earlier
  const entries = new Map(later.map((row) => [row.entry_id, row]))
  const tail = earlier.at(-1)
  const first = later[0]
  if (tail && first && compareHistoryPosition(first, tail) >= 0 && !earlier.some((row) => entries.has(row.entry_id))) {
    return [...earlier, ...entries.values()]
  }
  const prefix = [...new Map(earlier.filter((row) => !entries.has(row.entry_id)).map((row) => [row.entry_id, row])).values()]
  const newer = [...entries.values()]
  const merged: HistoryEntry[] = []
  let index = 0
  for (const row of newer) {
    while (index < prefix.length) {
      const previous = prefix[index]
      if (!previous || compareHistoryPosition(previous, row) > 0) break
      merged.push(previous)
      index++
    }
    merged.push(row)
  }
  return [...merged, ...prefix.slice(index)]
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
      const incoming = current.floor ? refresh.items.filter((row) => compareHistoryPosition(row, current.floor ?? row) >= 0) : refresh.items
      const tail = current.items.at(-1)
      const first = incoming[0]
      const incomingIds = new Set(incoming.map((row) => row.entry_id))
      if (tail && first && compareHistoryPosition(first, tail) >= 0 && !current.items.some((row) => incomingIds.has(row.entry_id))) {
        return { ...current, revision: refresh.revision, items: [...current.items, ...incoming] }
      }
      const parents = new Set(incoming.map((row) => row.message_id))
      const retained = current.items.filter((row) => row.message_id === undefined || !parents.has(row.message_id))
      const items = joinHistoryRanges(retained, incoming)
      return { ...current, revision: refresh.revision, items }
    }
    default: {
      const unreachable: never = refresh
      return unreachable
    }
  }
}

export function checkpointHistory(current: ChatHistory, page: ChatHistoryPage): ChatHistory {
  const first = page.items[0]
  if (!first) return current
  const pages = pageBoundaries(current)
  const latest = pages.at(-1)
  if (!latest || compareHistoryPosition(first, latest.first) <= 0) return current
  return { ...current, pages: [...pages, ...pageBoundaries(page)] }
}

export function prependHistory(current: ChatHistory, page: ChatHistoryPage): ChatHistory {
  const items = joinHistoryRanges(page.items, current.items)
  return { ...page, revision: current.revision, items, pages: [...pageBoundaries(page), ...pageBoundaries(current)],
    ...(current.floor && items[0] ? { floor: { created_at: items[0].created_at, message_id: items[0].message_id } } : {}) }
}

// Evict page groups only at a complete parent boundary. An oversized parent is
// deliberately retained whole; the newest page is never removed.
export function trimHistory(current: ChatHistory, protectedEntries: ReadonlySet<string> = new Set()): ChatHistory {
  if (current.items.length <= RETAINED_HISTORY_LIMIT) return current
  const pages = pageBoundaries(current)
  let cut = 0
  let pageIndex = 0
  for (let index = 1; index < pages.length; index++) {
    const page = pages[index]
    if (!page) continue
    const entryIndex = current.items.findIndex((row) => row.entry_id === page.first.entry_id)
    const start = entryIndex >= 0 ? entryIndex : current.items.findIndex((row) => compareHistoryPosition(row, page.first) >= 0)
    if (start <= 0) continue
    if (current.items.slice(0, start).some((row) => protectedEntries.has(row.entry_id))) break
    const previous = current.items[start - 1]
    const first = current.items[start]
    if (first?.message_id && previous?.message_id === first.message_id) continue
    cut = start
    pageIndex = index
    if (current.items.length - start <= RETAINED_HISTORY_LIMIT) break
  }
  const oldest = pages[pageIndex]
  const first = current.items[cut]
  if (!cut || !oldest || !first) return current
  return { ...current, items: current.items.slice(cut), pages: pages.slice(pageIndex), next_cursor: oldest.next_cursor,
    has_more: oldest.next_cursor !== null, floor: { created_at: first.created_at, message_id: first.message_id } }
}
