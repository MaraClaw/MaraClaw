import { apiUrl } from './api.ts'
import { authSession } from './auth-session.ts'
import type { AuthSessionStore, SessionOwner } from './auth-session.ts'

export class StaleSessionError extends Error {
  readonly name = 'StaleSessionError'
  readonly owner: SessionOwner

  constructor(owner: SessionOwner) {
    super('Request belongs to an obsolete session')
    this.owner = owner
  }
}

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

export function userFacingRequestError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback
  if (error.status >= 500) return fallback
  const detail = formatApiDetail(error.detail)
  if (!detail) return fallback
  if (/^request failed \(\d+\)$/i.test(detail) || detail.startsWith('<') || detail.length > 180) {
    return fallback
  }
  return detail
}

export function formatApiDetail(detail: unknown): string | null {
  if (detail == null) return null
  if (typeof detail === 'string') return detail
  if (typeof detail === 'object') {
    if ('message' in detail && typeof detail.message === 'string') return detail.message
    if (!('detail' in detail)) return null
    if (typeof detail.detail === 'string') return detail.detail
    if (Array.isArray(detail.detail)) {
      return detail.detail
        .map((item: unknown) => {
          if (typeof item === 'string') return item
          if (item && typeof item === 'object' && 'msg' in item) {
            return String(item.msg)
          }
          return null
        })
        .filter(Boolean)
        .join(' ')
    }
  }
  return null
}

export type RequestOptions = {
  readonly method?: string
  readonly body?: unknown
  readonly token?: string | null
  /** Pass the original owner when a token was captured by earlier work. */
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export function createHttpClient(
  session: Pick<AuthSessionStore, 'capture' | 'getSnapshot' | 'isCurrent' | 'expire'>,
  fetchRequest: typeof fetch = (...args) => fetch(...args),
  url: (path: string) => string = apiUrl,
) {
  const assertCurrent = (owner: SessionOwner): void => {
    if (!session.isCurrent(owner)) throw new StaleSessionError(owner)
  }

  async function execute(path: string, options: RequestOptions, form?: FormData): Promise<unknown> {
    const owner = options.owner ?? session.capture()
    const token = options.token === undefined ? session.getSnapshot().token : options.token
    assertCurrent(owner)
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (token) headers['Authorization'] = `Bearer ${token}`
    let body: BodyInit | undefined = form
    if (!form && options.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(options.body)
    }
    let response: Response
    let text: string
    try {
      response = await fetchRequest(url(path), {
        method: options.method ?? (form || options.body !== undefined ? 'POST' : 'GET'),
        headers,
        ...(body === undefined ? {} : { body }),
        ...(options.signal === undefined ? {} : { signal: options.signal }),
      })
      assertCurrent(owner)
      if (response.status === 204) return undefined
      text = await response.text()
    } catch (error) {
      assertCurrent(owner)
      throw error
    }
    assertCurrent(owner)
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error
        data = text
      }
    }
    if (!response.ok) {
      const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : data
      if (response.status === 401 && token) session.expire(owner)
      throw new ApiError(response.status, detail)
    }
    return data
  }

  return {
    async apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
      // Existing generic transport contract: domain callers own response schema parsing.
      return await execute(path, options) as T
    },
    async apiFormRequest<T>(
      path: string, form: FormData, options: Omit<RequestOptions, 'body'> = {},
    ): Promise<T> {
      return await execute(path, options, form) as T
    },
  }
}

export const { apiRequest, apiFormRequest } = createHttpClient(authSession)
