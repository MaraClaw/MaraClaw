import type { ChatMessage } from './message.ts'

export type ChatLine = ChatMessage & {
  readonly pending?: boolean
  readonly fromHistory?: true
  readonly composerText?: string
}

function sameUserLine(saved: ChatLine, pending: ChatLine): boolean {
  if (saved.role !== 'user') return false
  if (saved.content === pending.content) return true
  return Boolean(pending.file_name && saved.content.startsWith(`[file:${pending.file_name}]`))
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
    if (row.pending && row.role === 'user') {
      const match = serverUsers.findIndex((saved, index) => !used.has(index) && sameUserLine(saved, row))
      if (match >= 0) used.add(match)
      else pending.push(row)
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
