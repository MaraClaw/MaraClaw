import { apiRequest } from '@/lib/http'

export type DirectoryUser = {
  id: string
  display_name: string
  username?: string | null
  email?: string | null
  title?: string | null
  role: string
  avatar_url?: string | null
}

export type DirectoryMember = {
  id: string
  name: string
  email?: string | null
  title?: string | null
  department_path?: string | null
  avatar_url?: string | null
  provider_name?: string | null
  provider_type?: string | null
}

export type DirectoryDepartment = {
  id: string
  name: string
  path?: string | null
  parent_id?: string | null
  member_count?: number
  provider_name?: string | null
}

export async function listOrgUsers(): Promise<DirectoryUser[]> {
  return apiRequest('/api/org/users')
}

export async function listOrgMembers(search = ''): Promise<DirectoryMember[]> {
  const suffix = search ? `?search=${encodeURIComponent(search)}` : ''
  return apiRequest(`/api/enterprise/org/members${suffix}`)
}

export async function listOrgDepartments(): Promise<{ items: DirectoryDepartment[]; total_member?: number }> {
  return apiRequest('/api/enterprise/org/departments')
}
