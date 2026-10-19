import type { ChatLine } from './transcript.ts'
import type { ChatTurnRejection } from './ws-client.ts'

const recoverySteps = {
  quota_exceeded: 'Ask an admin to check your message allowance.',
  agent_expired: 'Ask an admin to renew this agent.',
  no_company_model: 'Ask an admin to set up a company model.',
  enqueue_failed: 'Try sending again.',
} satisfies Record<ChatTurnRejection['reason'], string>

export function recoverRejectedTurn(lines: readonly ChatLine[], rejection: ChatTurnRejection): {
  readonly lines: ChatLine[]
  readonly restoredText: string
  readonly notice: string
} {
  const index = lines.findLastIndex((line) => line.role === 'user' && line.pending)
  const rejected = lines[index]
  const attachmentStep = rejected?.file_name ? ` Attach ${rejected.file_name} again to resend the file.` : ''
  return {
    lines: lines.filter((_line, position) => position !== index),
    restoredText: rejected?.composerText ?? rejected?.content ?? '',
    notice: `Message not sent. ${rejection.content} ${recoverySteps[rejection.reason]}${attachmentStep}`,
  }
}

export function restoreComposerDraft(current: string, recovered: string): string {
  if (!recovered) return current
  if (!current) return recovered
  return `${recovered}\n\n${current}`
}
