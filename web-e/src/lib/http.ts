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
  readonly cache?: RequestCache
  readonly idempotencyKey?: string | undefined
}

export const MAX_PREVIEW_BYTES = 20 * 1024 * 1024

export class BinaryLimitError extends Error {
  readonly name = 'BinaryLimitError'
  constructor() { super('Preview is limited to files up to 20 MiB. Download this file to view it.') }
}

async function readPreviewBytes(response: Response, assertActive: () => void): Promise<Uint8Array> {
  const reader = response.body?.getReader()
  if (!reader) return new Uint8Array()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    if (Number(response.headers.get('Content-Length')) > MAX_PREVIEW_BYTES) throw new BinaryLimitError()
    while (true) {
      assertActive()
      const chunk = await reader.read()
      assertActive()
      if (chunk.done) break
      size += chunk.value.byteLength
      if (size > MAX_PREVIEW_BYTES) throw new BinaryLimitError()
      chunks.push(chunk.value)
    }
  } catch (error) {
    await reader.cancel(error)
    assertActive()
    throw error
  } finally {
    reader.releaseLock()
  }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return bytes
}

type ExecutionOptions = RequestOptions & { readonly form?: FormData; readonly binary?: true }

export function createHttpClient(
  session: Pick<AuthSessionStore, 'capture' | 'getSnapshot' | 'isCurrent' | 'expire'>,
  fetchRequest: typeof fetch = (...args) => fetch(...args),
  url: (path: string) => string = apiUrl,
) {
  const assertCurrent = (owner: SessionOwner): void => {
    if (!session.isCurrent(owner)) throw new StaleSessionError(owner)
  }

  async function execute(path: string, options: ExecutionOptions & { readonly binary: true }): Promise<Uint8Array>
  async function execute(path: string, options: ExecutionOptions): Promise<unknown>
  async function execute(path: string, options: ExecutionOptions): Promise<unknown> {
    const owner = options.owner ?? session.capture()
    const token = options.token === undefined ? session.getSnapshot().token : options.token
    assertCurrent(owner)
    const assertActive = () => { assertCurrent(owner); options.signal?.throwIfAborted() }
    const { form } = options
    const headers: Record<string, string> = { Accept: options.binary ? 'application/octet-stream' : 'application/json' }
    if (token) headers['Authorization'] = `Bearer ${token}`
    if (options.idempotencyKey !== undefined) headers['Idempotency-Key'] = options.idempotencyKey
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
        ...(options.cache === undefined ? {} : { cache: options.cache }),
        ...(options.binary ? { cache: 'no-store', redirect: 'error' } : {}),
      })
      assertActive()
      if (response.status === 204) return options.binary ? new Uint8Array() : undefined
      if (options.binary) {
        const bytes = await readPreviewBytes(response, assertActive)
        assertActive()
        if (response.ok) return bytes
        text = new TextDecoder().decode(bytes)
      } else text = await response.text()
    } catch (error) {
      assertCurrent(owner)
      throw error
    }
    assertCurrent(owner)
    options.signal?.throwIfAborted()
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
      return await execute(path, { ...options, form }) as T
    },
    apiBinaryRequest(path: string, options: Omit<RequestOptions, 'body'> = {}): Promise<Uint8Array> {
      return execute(path, { ...options, binary: true })
    },
  }
}

export const { apiRequest, apiFormRequest, apiBinaryRequest } = createHttpClient(authSession)
