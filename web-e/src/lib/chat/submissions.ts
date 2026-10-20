import type { SessionOwner } from '../auth-session.ts'
import type { ChatMessage } from './message.ts'
import type { ChatSubmission, ChatTurnAcceptance } from './protocol.ts'

export function createChatSubmissions(owner: SessionOwner, agentId: string, sessionId: string | undefined) {
  const envelopes = new Map<string, ChatSubmission>()
  const unknown = new Set<string>()
  const accepted = new Set<string>()
  let canonicalId = sessionId
  let active = true

  return {
    get active() { return active },
    get sessionId() { return canonicalId },
    matches(candidate: SessionOwner, agent: string, session: string | undefined) {
      return active && candidate === owner && agent === agentId && (session === sessionId || session === canonicalId)
    },
    resolve(session: string) { canonicalId = session },
    submit(payload: ChatSubmission): ChatSubmission | undefined {
      if (!active || accepted.has(payload.client_submission_id)) return
      const frozen = envelopes.get(payload.client_submission_id) ?? Object.freeze({ ...payload })
      envelopes.set(payload.client_submission_id, frozen)
      unknown.add(payload.client_submission_id)
      return frozen
    },
    pending() {
      return [...envelopes.values()].filter((payload) => unknown.has(payload.client_submission_id))
    },
    accept(receipt: ChatTurnAcceptance) {
      if (!active || receipt.session_id !== canonicalId || accepted.has(receipt.client_submission_id)
        || !envelopes.has(receipt.client_submission_id)) return false
      unknown.delete(receipt.client_submission_id)
      accepted.add(receipt.client_submission_id)
      return true
    },
    reconcile(history: readonly ChatMessage[]) {
      if (!active) return
      for (const row of history) {
        if (row.role !== 'user' || !row.client_submission_id || !row.message_id) continue
        unknown.delete(row.client_submission_id)
        accepted.add(row.client_submission_id)
      }
    },
    reject(id: string) { unknown.delete(id) },
    retire() {
      active = false
      envelopes.clear()
      unknown.clear()
      accepted.clear()
    },
  }
}

export type ChatSubmissions = ReturnType<typeof createChatSubmissions>
