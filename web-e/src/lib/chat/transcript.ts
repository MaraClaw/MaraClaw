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

function sameUserLine(saved: ChatLine, pending: ChatLine): boolean {
  if (saved.role !== 'user') return false
  return Boolean((saved.client_submission_id && saved.client_submission_id === pending.client_submission_id)
    || (saved.message_id && saved.message_id === pending.message_id))
}

export function acceptSubmission(local: readonly ChatLine[], receipt: ChatTurnAcceptance): ChatLine[] {
  return local.map((row) => row.role === 'user' && row.client_submission_id === receipt.client_submission_id
    ? { ...row, message_id: receipt.message_id, pending: false, delivery: 'accepted' } : row)
}

export function mergeServerHistory(server: readonly ChatMessage[], local: readonly ChatLine[]): ChatLine[] {
  const parents = new Set(server.flatMap((row) => row.message_id ? [row.message_id] : []))
  const entries = new Set<string>()
  const authoritative: ChatLine[] = []
  for (const row of server) {
    if (row.entry_id && entries.has(row.entry_id)) continue
    if (row.entry_id) entries.add(row.entry_id)
    authoritative.push({ ...row, fromHistory: true })
  }
  const serverUsers = server.filter((row) => row.role === 'user')
  const used = new Set<number>()
  const pending: ChatLine[] = []
  const older: ChatLine[] = []
  const live: ChatLine[] = []
  for (const row of local) {
    if (row.role === 'user' && (row.client_submission_id || row.pending)) {
      const match = serverUsers.findIndex((saved, index) => !used.has(index) && sameUserLine(saved, row))
      if (match >= 0) used.add(match)
      else (row.fromHistory ? older : pending).push(row)
    } else if (row.message_id) {
      if (!parents.has(row.message_id)) (row.fromHistory ? older : live).push(row)
    } else if (!row.pending && (row.role === 'assistant' || row.role === 'tool') &&
      !server.some((saved) => saved.role === row.role && saved.content === row.content)) {
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
