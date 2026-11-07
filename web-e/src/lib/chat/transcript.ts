import type { ChatMessage } from './message.ts'
import type { ChatSubmission, ChatTurnAcceptance } from './protocol.ts'

export type ChatLine = ChatMessage & {
  readonly pending?: boolean
  readonly fromHistory?: true
  readonly composerText?: string
  readonly submission?: ChatSubmission
  readonly delivery?: 'unknown' | 'accepted' | 'rejected'
  readonly retryable?: boolean
  readonly rejectionNotice?: string
}

function sameValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') return false
  if (Array.isArray(left) !== Array.isArray(right)) return false
  const leftEntries = Object.entries(left)
  const rightEntries = new Map(Object.entries(right))
  return leftEntries.length === rightEntries.size
    && leftEntries.every(([key, value]) => rightEntries.has(key) && sameValue(value, rightEntries.get(key)))
}

type MatchQueue = { readonly indices: number[]; next: number }
function firstUnused(queue: MatchQueue | undefined, used: ReadonlySet<number>): number {
  if (!queue) return Infinity
  while (queue.next < queue.indices.length && used.has(queue.indices[queue.next] ?? Infinity)) queue.next++
  return queue.indices[queue.next] ?? Infinity
}

function legacyKey(row: ChatMessage): string {
  return JSON.stringify([row.message_id, row.role, row.content])
}

export function acceptSubmission(local: readonly ChatLine[], receipt: ChatTurnAcceptance): ChatLine[] {
  return local.map((row) => row.role === 'user' && row.client_submission_id === receipt.client_submission_id
    ? { ...row, message_id: receipt.message_id, pending: false, delivery: 'accepted' } : row)
}

export function mergeServerHistory(server: readonly ChatMessage[], local: readonly ChatLine[]): ChatLine[] {
  const parents = new Set(server.flatMap((row) => row.message_id ? [row.message_id] : []))
  const previousEntries = new Map(local.filter((row) => row.entry_id).map((row) => [row.entry_id, row]))
  const previousLegacy = new Map<string, { readonly rows: ChatLine[]; next: number }>()
  for (const row of local) {
    if (row.entry_id) continue
    const key = legacyKey(row)
    const queue = previousLegacy.get(key) ?? { rows: [], next: 0 }
    queue.rows.push(row)
    previousLegacy.set(key, queue)
  }
  const submissions = new Map<string, MatchQueue>()
  const messages = new Map<string, MatchQueue>()
  const contents = new Map<string, Set<string>>()
  for (const [index, row] of server.entries()) {
    const roleContents = contents.get(row.role) ?? new Set<string>()
    roleContents.add(row.content)
    contents.set(row.role, roleContents)
    if (row.role !== 'user') continue
    for (const [lookup, id] of [[submissions, row.client_submission_id], [messages, row.message_id]] as const) {
      if (!id) continue
      const queue = lookup.get(id) ?? { indices: [], next: 0 }
      queue.indices.push(index)
      lookup.set(id, queue)
    }
  }
  const entries = new Set<string>()
  const authoritative: ChatLine[] = []
  for (const row of server) {
    if (row.entry_id && entries.has(row.entry_id)) continue
    if (row.entry_id) entries.add(row.entry_id)
    const next: ChatLine = { ...row, fromHistory: true }
    const legacy = row.entry_id ? undefined : previousLegacy.get(legacyKey(row))
    const previous = row.entry_id ? previousEntries.get(row.entry_id) : legacy?.rows[legacy.next++]
    authoritative.push(previous && sameValue(previous, next) ? previous : next)
  }
  const used = new Set<number>()
  const pending: ChatLine[] = []
  const older: ChatLine[] = []
  const live: ChatLine[] = []
  for (const row of local) {
    if (row.role === 'user' && (row.client_submission_id || row.pending)) {
      const match = Math.min(firstUnused(submissions.get(row.client_submission_id ?? ''), used), firstUnused(messages.get(row.message_id ?? ''), used))
      if (match !== Infinity) used.add(match)
      else (row.fromHistory ? older : pending).push(row)
    } else if (row.message_id) {
      if (!parents.has(row.message_id)) (row.fromHistory ? older : live).push(row)
    } else if (!row.pending && (row.role === 'assistant' || row.role === 'tool') &&
      !contents.get(row.role)?.has(row.content)) {
      live.push(row)
    }
  }
  return [...older, ...authoritative, ...pending, ...live]
}

export function appendCompletion(local: readonly ChatLine[], completion: ChatMessage): ChatLine[] {
  if (completion.message_id) {
    const parent = local.filter((row) => row.message_id === completion.message_id)
    if (parent.some((row) => row.entry_id === completion.entry_id || row.fromHistory)) return [...local]
  } else {
    const last = local.at(-1)
    if (!last?.message_id && last?.role === completion.role && last.content === completion.content) return [...local]
  }
  return [...local, completion]
}
