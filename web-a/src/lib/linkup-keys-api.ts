import { apiRequest } from '@/lib/http'

export type LinkupKey = {
  id: string
  label: string
  fingerprint: string
  position: number
  status: string
  exhausted_until: string | null
  last_used_at: string | null
  created_at: string | null
}

export type CreateLinkupKeyInput = {
  label: string
  api_key: string
}

export async function listLinkupKeys(): Promise<LinkupKey[]> {
  return apiRequest<LinkupKey[]>('/api/admin/linkup-keys')
}

export async function createLinkupKey(input: CreateLinkupKeyInput): Promise<LinkupKey> {
  return apiRequest<LinkupKey>('/api/admin/linkup-keys', {
    method: 'POST',
    body: input,
  })
}

export async function deleteLinkupKey(keyId: string): Promise<LinkupKey> {
  return apiRequest<LinkupKey>(`/api/admin/linkup-keys/${keyId}`, { method: 'DELETE' })
}
