export function apiUrl(path: string): string {
  const raw = import.meta.env.VITE_API_BASE_URL
  const base = typeof raw === 'string' && raw.trim() ? raw.replace(/\/$/, '') : ''
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

const TOKEN_KEY = 'maraclaw-enduser-token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function apiRequest<T>(path: string, options: { method?: string; body?: unknown; token?: string | null } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = options.token === undefined ? getToken() : options.token
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(apiUrl(path), {
    method: options.method ?? (options.body !== undefined ? 'POST' : 'GET'),
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })
  if (response.status === 204) return undefined as T
  const data = (await response.json().catch(() => null)) as { detail?: unknown; message?: string } | null
  if (!response.ok) {
    const detail = data?.detail ?? data?.message ?? `Request failed (${response.status})`
    throw new ApiError(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data as T
}
