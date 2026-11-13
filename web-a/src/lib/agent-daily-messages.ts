import { ApiError } from './http.ts'
import type { UserOut } from './types/auth.ts'
import type { AdminUser } from './users-api.ts'

/** Mirrors the engine: platform admins reach any organization, org admins only their own. */
export function canResetAgentDailyMessages(viewer: UserOut | null | undefined, owner: Pick<AdminUser, 'tenant_id'>): boolean {
  if (viewer?.role === 'platform_admin') return true
  return viewer?.role === 'org_admin' && viewer.tenant_id != null && viewer.tenant_id === owner.tenant_id
}

export function agentDailyMessagesError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'Your permissions have changed. Reload this page before trying again.'
    if (error.status === 404) return 'This agent was not found for this person. Reload this page to see the current agents.'
    if (error.status === 409) return 'This agent no longer has a daily message limit. Reload this page to see the current limits.'
  }
  return 'Unable to reset daily messages. Check your connection and try again.'
}
