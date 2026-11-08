import type { ChatLine } from './transcript.ts'

export type TranscriptItem = { readonly id: string; readonly index: number; readonly top: number; readonly height: number }
export type TranscriptAnchor = { readonly id: string; readonly offset: number }
export type TranscriptLayout = { readonly rows: readonly TranscriptItem[]; readonly byId: ReadonlyMap<string, TranscriptItem>; readonly height: number }
const localIds = new WeakMap<ChatLine, string>()
let localId = 0

export function transcriptEntryId(line: ChatLine): string {
  const id = line.entry_id ?? line.client_submission_id ?? line.message_id
  if (id) return id
  const held = localIds.get(line)
  if (held) return held
  const next = `local-${++localId}`
  localIds.set(line, next)
  return next
}

export function transcriptLayout(lines: readonly ChatLine[], heights: ReadonlyMap<string, number>): TranscriptLayout {
  let top = 0
  const rows = lines.map((line, index) => {
    const id = transcriptEntryId(line)
    const height = heights.get(id) ?? 88
    const row = { id, index, top, height }
    top += height
    return row
  })
  return { rows, byId: new Map(rows.map((row) => [row.id, row])), height: top }
}

export function firstVisibleRow(rows: readonly TranscriptItem[], top: number): number {
  let low = 0
  let high = rows.length
  while (low < high) {
    const middle = (low + high) >>> 1
    const row = rows[middle]
    if (row && row.top + row.height <= top) low = middle + 1
    else high = middle
  }
  return low
}

export function transcriptWindow(layout: TranscriptLayout, viewport: { readonly top: number; readonly height: number }, pinned: ReadonlySet<string>): readonly TranscriptItem[] {
  const start = Math.max(0, firstVisibleRow(layout.rows, viewport.top) - 8)
  const end = Math.min(layout.rows.length, firstVisibleRow(layout.rows, viewport.top + viewport.height) + 9)
  const rows = new Map(layout.rows.slice(start, end).map((row) => [row.id, row]))
  for (const id of pinned) {
    const row = layout.byId.get(id)
    if (row) rows.set(id, row)
  }
  return [...rows.values()].sort((left, right) => left.index - right.index)
}

export function anchoredScrollTop(anchor: TranscriptAnchor, layout: TranscriptLayout, origin: number): number | undefined {
  const row = layout.byId.get(anchor.id)
  return row ? origin + row.top - anchor.offset : undefined
}
