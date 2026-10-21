import { apiRequest } from '@/lib/http'

export type DirectoryUser = {
  id: string
  display_name: string
  email?: string | null
  title?: string | null
  role: string
  avatar_url?: string | null
}

export async function listOrgUsers(): Promise<DirectoryUser[]> {
  return apiRequest('/api/org/users')
}
