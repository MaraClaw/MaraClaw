import type { ChatLine } from './transcript.ts'
import type { ChatTurnRejection } from './ws-client.ts'

const recoverySteps = {
  quota_exceeded: 'Ask an admin to check your message allowance.',
  agent_expired: 'Ask an admin to renew this agent.',
  no_company_model: 'Ask an admin to set up a company model.',
  enqueue_failed: 'Retry this message.',
  guest_config_failed: "Wait a moment, then retry. If the problem continues, ask an admin to check this agent's configuration.",
  invalid_submission_id: 'Start a new message.',
  submission_conflict: 'Start a new message instead of retrying this submission.',
  invalid_attachments: 'Upload the attachment again, then send a new message.',
  openclaw_update_required: "Open this agent's Settings and choose Upgrade, then retry once the upgrade finishes.",
  openclaw_update_in_progress: "Open this agent's Settings to check Upgrade progress, then retry once the upgrade finishes.",
} satisfies Record<ChatTurnRejection['reason'], string>

export function recoverRejectedTurn(lines: readonly ChatLine[], rejection: ChatTurnRejection): {
  readonly lines: ChatLine[]
  readonly restoredText: string
  readonly notice: string
  readonly matched: boolean
} {
  if (rejection.client_submission_id === undefined) return {
    lines: [...lines], restoredText: '', matched: false,
    notice: `Onboarding could not start. ${rejection.content} ${rejection.reason === 'enqueue_failed'
      ? 'Ask an admin to check this agent.' : recoverySteps[rejection.reason]}`,
  }
  const index = lines.findIndex((line) => line.role === 'user' && line.pending && line.delivery !== 'accepted'
    && line.client_submission_id === rejection.client_submission_id)
  const rejected = lines[index]
  const attachmentStep = rejected?.file_name && rejection.reason !== 'invalid_attachments'
    ? ` Retry uses the uploaded ${rejected.file_name}; no upload is needed.` : ''
  const notice = `Message not sent. ${rejection.content} ${recoverySteps[rejection.reason]}${attachmentStep}`
  return {
    lines: lines.map((line, position) => position === index ? { ...line, pending: false, delivery: 'rejected',
      rejectionNotice: notice, retryable: rejection.reason !== 'invalid_submission_id' && rejection.reason !== 'submission_conflict'
        && rejection.reason !== 'invalid_attachments' } : line),
    restoredText: rejected?.composerText ?? rejected?.content ?? '',
    notice,
    matched: index >= 0,
  }
}

export function restoreComposerDraft(current: string, recovered: string): string {
  if (!recovered) return current
  if (!current) return recovered
  return `${recovered}\n\n${current}`
}
