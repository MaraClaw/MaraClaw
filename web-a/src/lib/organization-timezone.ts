import { ApiError } from './http.ts'
import { isPlatformAdminUser, type UserOut } from './types/auth.ts'

/** Only the organization's genesis org admin owns the clock for its agents' daily message limits. */
export function canManageOrganizationTimezone(user: UserOut | null | undefined): boolean {
  return user?.role === 'org_admin' && user.is_genesis === true && !isPlatformAdminUser(user)
    && user.tenant_id != null && user.is_active
}

export function organizationTimezoneError(error: unknown): string {
  if (error instanceof ApiError && error.status === 422) return 'Choose a valid timezone.'
  return 'Unable to save the timezone. Check your connection and try again.'
}
