import { agentDeletionDetailSchema } from '../src/lib/agent-deletion-api.ts'
import type { AdminUser } from '../src/lib/users-api.ts'

export const sourceId = '7c84cb77-6adf-4ad8-a69d-96992a92c0b9'
export const targetId = 'd81038c8-b808-44f0-af67-f5d45f633c14'
export const agentId = '0dbefca9-3290-44be-a8be-627395ca4217'
export const tenantId = '00870c73-1512-4f23-bb7a-f0a301c51a5e'
export const deletionRequest = agentDeletionDetailSchema.parse({
  id: '94344f06-11eb-413c-b15d-a7bde2a3ede3', agent_id: agentId, agent_name: 'Research agent',
  status: 'pending', cancel_reason: null,
  requester: { id: sourceId, display_name: 'Source member' },
  created_at: '2026-10-25T00:00:00Z', resolved_at: null,
  progress: { counted_approvals: 0, required_approvals: 2, genesis_approved: false },
  reason: 'No longer needed',
  agent: {
    id: agentId, name: 'Research agent', role_description: 'Research', access_mode: 'private',
    status: 'stopped', last_active_at: null, creator: { id: sourceId, display_name: 'Source member' }, is_private: true,
  },
  viewer: { can_approve: true, has_viewed: true, has_approved: false }, approvals: [],
})
export const approvedDeletion = agentDeletionDetailSchema.parse({
  ...deletionRequest, viewer: { can_approve: false, has_viewed: true, has_approved: true },
  progress: { counted_approvals: 1, required_approvals: 2, genesis_approved: false },
  approvals: [{ reviewer_id: targetId, display_name: 'Reviewer', is_genesis: false, approved_at: '2026-10-25T01:00:00Z', counted: true }],
})
export const sourceMember: AdminUser = {
  id: sourceId, tenant_id: tenantId, role: 'member', is_active: false,
  display_name: 'Source member', email: 'source@example.test', agents_count: 1,
  source: 'member', created_at: null,
}
export const targetMember: AdminUser = { ...sourceMember, id: targetId, is_active: true, display_name: 'Recipient' }
