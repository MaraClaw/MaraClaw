import { apiUrl } from '@/lib/api'
import { clearStoredToken, getStoredToken } from '@/lib/auth-storage'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? formatApiDetail(detail) ?? `Request failed (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function formatApiDetail(detail: unknown): string | null {
  if (detail == null) return null
  if (typeof detail === 'string') return detail
  if (typeof detail === 'object') {
    const obj = detail as Record<string, unknown>
    if (typeof obj.message === 'string') return obj.message
    if (typeof obj.detail === 'string') return obj.detail
    if (Array.isArray(obj.detail)) {
      return obj.detail
        .map((item) => {
          if (typeof item === 'string') return item
          if (item && typeof item === 'object' && 'msg' in item) {
            return String((item as { msg: unknown }).msg)
          }
          return null
        })
        .filter(Boolean)
        .join(' ')
    }
  }
  return null
}

type RequestOptions = {
  method?: string
  body?: unknown
  token?: string | null
  signal?: AbortSignal
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
  }

  const token = options.token === undefined ? getStoredToken() : options.token
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  const response = await fetch(apiUrl(path), {
    method: options.method ?? (options.body !== undefined ? 'POST' : 'GET'),
    headers,
    body,
    signal: options.signal,
  })

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text) as unknown
    } catch {
      data = text
    }
  }

  if (!response.ok) {
    const detail =
      data && typeof data === 'object' && data !== null && 'detail' in data
        ? (data as { detail: unknown }).detail
        : data

    if (response.status === 401 && token) {
      clearStoredToken()
    }

    throw new ApiError(response.status, detail)
  }

  return data as T
}

export async function apiFormRequest<T>(
  path: string,
  form: FormData,
  options: Omit<RequestOptions, 'body'> = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
  }
  const token = options.token === undefined ? getStoredToken() : options.token
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(apiUrl(path), {
    method: options.method ?? 'POST',
    headers,
    body: form,
    signal: options.signal,
  })

  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text) as unknown
    } catch {
      data = text
    }
  }

  if (!response.ok) {
    const detail =
      data && typeof data === 'object' && data !== null && 'detail' in data
        ? (data as { detail: unknown }).detail
        : data
    if (response.status === 401 && token) {
      clearStoredToken()
    }
    throw new ApiError(response.status, detail)
  }

  return data as T
}
