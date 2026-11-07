import type { ChatMessage } from '../src/lib/chat/message.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'

// Frozen pre-optimization merge, kept as the independent differential oracle.
export function referenceMerge(server: readonly ChatMessage[], local: readonly ChatLine[]): ChatLine[] {
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
      const match = serverUsers.findIndex((saved, index) => !used.has(index)
        && Boolean((saved.client_submission_id && saved.client_submission_id === row.client_submission_id)
          || (saved.message_id && saved.message_id === row.message_id)))
      if (match >= 0) used.add(match)
      else (row.fromHistory ? older : pending).push(row)
    } else if (row.message_id) {
      if (!parents.has(row.message_id)) (row.fromHistory ? older : live).push(row)
    } else if (!row.pending && (row.role === 'assistant' || row.role === 'tool')
      && !server.some((saved) => saved.role === row.role && saved.content === row.content)) {
      live.push(row)
    }
  }
  return [...older, ...authoritative, ...pending, ...live]
}
