import { apiRequest } from '@/lib/http'

export type UserAgent = {
  id: string
  name: string
  status: string
  is_expired: boolean
  role_description: string | null
  last_active_at: string | null
}

export type UserDetail = AdminUser & {
  username: string | null
  quota_message_limit?: number
  quota_message_period?: string
  quota_messages_used?: number
  quota_max_agents?: number
  quota_agent_ttl_hours?: number
  agents: UserAgent[]
}

export type AdminUser = {
  id: string
  username: string | null
  email: string | null
  display_name: string | null
  role: string
  is_active: boolean
  is_genesis?: boolean
  tenant_id: string | null
  agents_count: number
  created_at: string | null
  source: string
}

export type PlatformAdmin = {
  id: string
  email: string | null
  display_name: string | null
  role: string
  is_active: boolean
  is_genesis: boolean
  created_at: string | null
}

export function isEndUserRole(role: string): boolean {
  return role === 'member' || role === 'agent_admin'
}

export function roleLabel(role: string): string {
  if (role === 'org_admin') return 'Org admin'
  if (role === 'platform_admin') return 'Platform admin'
  if (role === 'agent_admin') return 'Agent admin'
  return 'Member'
}

export function agentStatusLabel(status: string, expired: boolean): string {
  if (expired) return 'Expired'
  if (status === 'running') return 'Running'
  if (status === 'stopped' || status === 'idle') return 'Stopped'
  if (status === 'error') return 'Error'
  if (status === 'creating') return 'Creating'
  return status
}

export function asAdminUser(admin: PlatformAdmin): AdminUser {
  return {
    id: admin.id,
    username: null,
    email: admin.email,
    display_name: admin.display_name,
    role: admin.role || 'platform_admin',
    is_active: admin.is_active,
    is_genesis: admin.is_genesis,
    tenant_id: null,
    agents_count: 0,
    created_at: admin.created_at,
    source: 'platform_admin',
  }
}

export async function listUsers(tenantId?: string): Promise<AdminUser[]> {
  const suffix = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return apiRequest<AdminUser[]>(`/api/users/${suffix}`)
}

export async function getUserDetail(userId: string): Promise<UserDetail> {
  return apiRequest<UserDetail>(`/api/users/${userId}`)
}

export async function listPlatformAdmins(): Promise<PlatformAdmin[]> {
  return apiRequest<PlatformAdmin[]>('/api/admin/platform-admins')
}

export async function setUserActive(userId: string, isActive: boolean): Promise<AdminUser> {
  return apiRequest<AdminUser>(`/api/users/${userId}/active`, {
    method: 'PATCH',
    body: { is_active: isActive },
  })
}

export async function setPlatformAdminActive(userId: string, isActive: boolean): Promise<PlatformAdmin> {
  return apiRequest<PlatformAdmin>(`/api/admin/platform-admins/${userId}/active`, {
    method: 'PATCH',
    body: { is_active: isActive },
  })
}

export async function setOrgAdminActive(userId: string, isActive: boolean): Promise<AdminUser> {
  const updated = await apiRequest<{
    id: string
    email: string | null
    display_name: string | null
    role: string
    is_active: boolean
    is_genesis?: boolean
    tenant_id: string | null
    created_at: string | null
  }>(`/api/users/org-admins/${userId}/active`, {
    method: 'PATCH',
    body: { is_active: isActive },
  })
  return {
    id: updated.id,
    username: null,
    email: updated.email,
    display_name: updated.display_name,
    role: updated.role,
    is_active: updated.is_active,
    is_genesis: updated.is_genesis,
    tenant_id: updated.tenant_id,
    agents_count: 0,
    created_at: updated.created_at,
    source: 'org_admin',
  }
}
