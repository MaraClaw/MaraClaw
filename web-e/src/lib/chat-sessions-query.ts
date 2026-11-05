import type { SessionOut, SessionPageOut } from './engine-contracts.generated.ts'
import type { SessionListScope } from './chat-sessions-api.ts'

/** Chat pages one infinite list per agent and scope; the scope belongs to the key because the rows differ. */
export const chatSessionsKey = (agentId: string, scope: SessionListScope) => ['sessions', agentId, scope] as const
/** Take Control owns its list so it never reads (or seeds) the chat cache. */
export const controlSessionsKey = (agentId: string) => ['sessions', agentId, 'control', 'mine'] as const
/** A URL-opened session that is not on any loaded page. */
export const chatSessionKey = (agentId: string, sessionId: string) => ['sessions', agentId, 'item', sessionId] as const

/** Flatten loaded pages in order, keeping the first copy of any id a later page repeats. */
export function distinctSessions(pages: readonly Pick<SessionPageOut, 'items'>[] | undefined): SessionOut[] {
  const seen = new Set<string>()
  const rows: SessionOut[] = []
  for (const page of pages ?? []) {
    for (const session of page.items) {
      if (seen.has(session.id)) continue
      seen.add(session.id)
      rows.push(session)
    }
  }
  return rows
}
