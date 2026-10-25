import { isGenesisAdmin, type UserOut } from './types/auth.ts'

export function tenantEmailScope(user: UserOut | null | undefined): string | null {
  if (!user?.is_active || !isGenesisAdmin(user)) return null
  if (user.role !== 'org_admin' && user.role !== 'platform_admin') return null
  return user.tenant_id || null
}
