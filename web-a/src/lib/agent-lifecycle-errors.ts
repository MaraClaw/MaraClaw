import { z } from 'zod'

import { ApiError } from './http.ts'

const errorSchema = z.object({ code: z.enum([
  'detail_view_required', 'reviewer_not_eligible', 'deletion_request_stale',
  'transfer_retention_conflict', 'target_not_eligible', 'target_quota_exceeded',
  'agent_not_owned_by_source', 'transfer_workspace_busy',
]) })
const messages = {
  detail_view_required: 'Open and reload the request details before approving.',
  reviewer_not_eligible: 'You are no longer eligible to approve this request. Reload the details.',
  deletion_request_stale: 'This request has changed or is already resolved. Reload the details.',
  transfer_retention_conflict: 'These agents have incompatible retention settings. Review the selection and try again.',
  target_not_eligible: 'The recipient is no longer an active member of this organization. Choose another recipient.',
  target_quota_exceeded: 'The recipient has reached their agent quota. Choose another recipient or update their quota.',
  agent_not_owned_by_source: 'Agent ownership has changed. Reload the agents before transferring.',
  transfer_workspace_busy: 'An agent workspace is busy. Wait for its current operation to finish and try again.',
} satisfies Record<z.infer<typeof errorSchema>['code'], string>

export function agentLifecycleError(error: unknown): string {
  if (error instanceof ApiError) {
    const parsed = errorSchema.safeParse(error.detail)
    if (parsed.success) return messages[parsed.data.code]
    if (error.status === 403) return 'Your permissions have changed. Reload this page before trying again.'
    if (error.status === 409) return 'The agents or request changed. Reload this page before trying again.'
  }
  return 'Unable to complete this operation. Check your connection and try again.'
}
