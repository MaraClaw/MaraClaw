import { apiUrl } from './api.ts'
import { adminSession } from './admin-session.ts'
import { StaleSessionError, type SessionPolicy } from './session-policy.ts'

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

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
  readonly method?: string
  readonly body?: unknown
  readonly token?: string | null
  readonly signal?: AbortSignal
}

type Transport = {
  readonly fetch: typeof globalThis.fetch
  readonly url: (path: string) => string
}

export function createApiRequest(policy: SessionPolicy, transport: Transport) {
  return async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    const current = policy.capture()
    const token = options.token === undefined ? current.token : options.token
    const owner = token ? { ...current, token } : null
    if (owner) policy.assertCurrent(owner)
    const signal = owner
      ? (options.signal ? AbortSignal.any([owner.signal, options.signal]) : owner.signal)
      : options.signal
    if (token) headers.Authorization = `Bearer ${token}`

    let body: string | undefined
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(options.body)
    }

    function assertOwner() {
      if (owner) policy.assertCurrent(owner)
      signal?.throwIfAborted()
    }

    try {
      assertOwner()
      const response = await transport.fetch(transport.url(path), {
        method: options.method ?? (options.body !== undefined ? 'POST' : 'GET'),
        headers,
        body,
        signal,
      })
      assertOwner()
      if (response.status === 204) return undefined as T

      const text = await response.text()
      assertOwner()
      let data: unknown = null
      if (text) {
        try {
          data = JSON.parse(text) as unknown
        } catch (error) {
          if (!(error instanceof SyntaxError)) throw error
          data = text
        }
      }

      if (!response.ok) {
        const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : data
        if (response.status === 401 && owner) policy.expire(owner)
        throw new ApiError(response.status, detail)
      }
      return data as T
    } catch (error) {
      // Expiry has already notified auth; preserve that request's ApiError.
      if (error instanceof ApiError && error.status === 401) throw error
      if (owner && !policy.isCurrent(owner)) throw new StaleSessionError()
      signal?.throwIfAborted()
      throw error
    }
  }
}

export const apiRequest = createApiRequest(adminSession, {
  fetch: (...args) => globalThis.fetch(...args),
  url: apiUrl,
})
