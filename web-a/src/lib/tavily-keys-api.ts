import { apiRequest } from '@/lib/http'

export type TavilyKey = {
  id: string
  label: string
  fingerprint: string
  position: number
  status: string
  exhausted_until: string | null
  last_used_at: string | null
  created_at: string | null
}

export type CreateTavilyKeyInput = {
  label: string
  api_key: string
}

export async function listTavilyKeys(signal?: AbortSignal): Promise<TavilyKey[]> {
  return apiRequest<TavilyKey[]>('/api/admin/tavily-keys', { signal })
}

export async function createTavilyKey(input: CreateTavilyKeyInput): Promise<TavilyKey> {
  return apiRequest<TavilyKey>('/api/admin/tavily-keys', {
    method: 'POST',
    body: input,
  })
}

export async function deleteTavilyKey(keyId: string): Promise<TavilyKey> {
  return apiRequest<TavilyKey>(`/api/admin/tavily-keys/${keyId}`, { method: 'DELETE' })
}
