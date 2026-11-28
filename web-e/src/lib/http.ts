import { apiUrl } from './api.ts'
import { authSession } from './auth-session.ts'
import type { AuthSessionStore, SessionOwner } from './auth-session.ts'
import type { TranslationKey } from './i18n/catalog.ts'
import { readLocale, translateOwned } from './i18n/translation.ts'

export class StaleSessionError extends Error {
  readonly name = 'StaleSessionError'
  readonly owner: SessionOwner

  constructor(owner: SessionOwner) {
    super('Request belongs to an obsolete session')
    this.owner = owner
  }
}

const REQUEST_TIMEOUT_MESSAGE = 'The server took too long to respond. Check your connection and try again.' satisfies TranslationKey

/** The request outlived its deadline. Unlike a caller's abort it is shown and retryable, and it says nothing about the session. */
export class RequestTimeoutError extends Error {
  readonly name = 'RequestTimeoutError'
  readonly timeoutMs: number

  constructor(timeoutMs: number, cause: unknown) {
    super(REQUEST_TIMEOUT_MESSAGE, { cause })
    this.timeoutMs = timeoutMs
  }
}

export class ApiError extends Error {
  status: number
  detail: unknown
  readonly retryAfterMs: number | null

  constructor(status: number, detail: unknown, message?: string, retryAfterMs: number | null = null) {
    super(formatApiDetail(message) ?? formatApiDetail(detail) ?? (status >= 500
      ? 'The service is temporarily unavailable. Please try again.' : `Request failed (${status})`))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.retryAfterMs = retryAfterMs
  }

  get isMemberAudienceRejection(): boolean {
    return this.status === 403 && this.detail !== null && typeof this.detail === 'object' &&
      'code' in this.detail && this.detail.code === 'member_audience_rejected'
  }
}

// Retry-After is a count of seconds or an HTTP-date (RFC 9110); anything else is ignored.
export function parseRetryAfter(value: string | null, now: number): number | null {
  const text = value?.trim()
  if (!text) return null
  if (/^\d+$/.test(text)) return Number(text) * 1000
  const date = /^[A-Za-z]{3}/.test(text) ? Date.parse(text) : Number.NaN
  return Number.isNaN(date) ? null : Math.max(0, date - now)
}

export function userFacingRequestError(error: unknown, fallback: string): string {
  if (error instanceof RequestTimeoutError) return translateOwned(readLocale(), REQUEST_TIMEOUT_MESSAGE)
  if (!(error instanceof ApiError)) return fallback
  // 507 is the engine's storage and workspace-disk quota refusal; its detail names what to free up.
  if (error.status >= 500 && error.status !== 507) return fallback
  const detail = formatApiDetail(error.detail)
  if (!detail) return fallback
  if (/^request failed \(\d+\)$/i.test(detail) || detail.startsWith('<') || detail.length > 180) {
    return fallback
  }
  return detail
}

export function formatApiDetail(detail: unknown): string | null {
  if (detail == null) return null
  if (typeof detail === 'string') {
    const text = detail.trim()
    return text && text.length <= 500 && !/<[^>]*>|&(?:lt|gt|#0*60|#x0*3c);/i.test(text) ? text : null
  }
  if (typeof detail === 'object') {
    if ('message' in detail && typeof detail.message === 'string') return formatApiDetail(detail.message)
    if (!('detail' in detail)) return null
    if (typeof detail.detail === 'string') return formatApiDetail(detail.detail)
    if (Array.isArray(detail.detail)) {
      return formatApiDetail(detail.detail
        .map((item: unknown) => {
          if (typeof item === 'string') return item
          if (item && typeof item === 'object' && 'msg' in item) {
            return String(item.msg)
          }
          return null
        })
        .filter(Boolean)
        .join(' '))
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
  /** Replaces the default deadline; covers the response body as well as the headers. */
  readonly timeoutMs?: number
}

export type ResponseSchema<T> = { readonly parse: (input: unknown) => T }

export type SchemaRequestOptions<T> = RequestOptions & { readonly schema: ResponseSchema<T> }

export const MAX_PREVIEW_BYTES = 20 * 1024 * 1024

/** Below the proxy's 120 s upstream read timeout and above every engine-side upstream call it may wait on (60 s at most). */
export const DEFAULT_REQUEST_TIMEOUT_MS = 90_000
/** The slowest sustained transfer a payload deadline allows for: a congested office uplink. */
const MIN_TRANSFER_BYTES_PER_SECOND = 64 * 1024

/** A deadline for moving `bytes`: the default for the server's part plus the payload at the slowest rate allowed for. */
export function transferTimeoutMs(bytes: number): number {
  return DEFAULT_REQUEST_TIMEOUT_MS + Math.ceil(bytes / MIN_TRANSFER_BYTES_PER_SECOND) * 1000
}

/** Settles like `work`, or rejects with the signal's reason once it aborts, even when the transport ignores the signal. */
function untilAborted<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => { reject(signal.reason) }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    work.then(resolve, reject).finally(() => { signal.removeEventListener('abort', abort) })
  })
}

export class BinaryLimitError extends Error {
  readonly name = 'BinaryLimitError'
  constructor() { super('Preview is limited to files up to 20 MiB. Download this file to view it.') }
}

async function readPreviewBytes(response: Response, assertActive: () => void, signal: AbortSignal): Promise<Uint8Array> {
  const reader = response.body?.getReader()
  if (!reader) return new Uint8Array()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    if (Number(response.headers.get('Content-Length')) > MAX_PREVIEW_BYTES) throw new BinaryLimitError()
    while (true) {
      assertActive()
      const chunk = await untilAborted(reader.read(), signal)
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

export type HttpTransport = {
  readonly fetch: typeof fetch
  readonly url: (path: string) => string
  readonly now: () => number
  /** The deadline signal for one request; tests inject one they abort themselves. */
  readonly deadline: (timeoutMs: number) => AbortSignal
}

const browserTransport: HttpTransport = {
  fetch: (...args) => fetch(...args),
  url: apiUrl,
  now: Date.now,
  deadline: (timeoutMs) => AbortSignal.timeout(timeoutMs),
}

export function createHttpClient(
  session: Pick<AuthSessionStore, 'capture' | 'getSnapshot' | 'isCurrent' | 'expire'>,
  overrides: Partial<HttpTransport> = {},
) {
  const transport: HttpTransport = { ...browserTransport, ...overrides }
  const assertCurrent = (owner: SessionOwner): void => {
    if (!session.isCurrent(owner)) throw new StaleSessionError(owner)
  }

  async function execute(path: string, options: ExecutionOptions & { readonly binary: true }): Promise<Uint8Array>
  async function execute(path: string, options: ExecutionOptions): Promise<unknown>
  async function execute(path: string, options: ExecutionOptions): Promise<unknown> {
    const owner = options.owner ?? session.capture()
    const token = options.token === undefined ? session.getSnapshot().token : options.token
    assertCurrent(owner)
    // Binary bodies are capped at MAX_PREVIEW_BYTES, so their default deadline is sized for that payload.
    const timeoutMs = options.timeoutMs ?? (options.binary ? transferTimeoutMs(MAX_PREVIEW_BYTES) : DEFAULT_REQUEST_TIMEOUT_MS)
    const deadline = transport.deadline(timeoutMs)
    const signal = options.signal ? AbortSignal.any([options.signal, deadline]) : deadline
    // Precedence: a retired session, then the caller's own abort, then the deadline.
    const assertActive = () => {
      assertCurrent(owner)
      options.signal?.throwIfAborted()
      if (deadline.aborted) throw new RequestTimeoutError(timeoutMs, deadline.reason)
    }
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
      response = await untilAborted(transport.fetch(transport.url(path), {
        method: options.method ?? (form || options.body !== undefined ? 'POST' : 'GET'),
        headers,
        ...(body === undefined ? {} : { body }),
        signal,
        ...(options.cache === undefined ? {} : { cache: options.cache }),
        ...(options.binary ? { cache: 'no-store', redirect: 'error' } : {}),
      }), signal)
      assertActive()
      if (response.status === 204) return options.binary ? new Uint8Array() : undefined
      if (options.binary) {
        const bytes = await readPreviewBytes(response, assertActive, signal)
        assertActive()
        if (response.ok) return bytes
        text = new TextDecoder().decode(bytes)
      } else text = await untilAborted(response.text(), signal)
    } catch (error) {
      assertActive()
      throw error
    }
    assertActive()
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
      const throttled = response.status === 429 || response.status === 503
      throw new ApiError(response.status, detail, undefined,
        throttled ? parseRetryAfter(response.headers.get('Retry-After'), transport.now()) : null)
    }
    return data
  }

  function apiRequest<T>(path: string, options: SchemaRequestOptions<T>): Promise<T>
  function apiRequest<T = unknown>(
    path: string, options?: RequestOptions & { readonly schema?: never },
  ): Promise<T>
  async function apiRequest(
    path: string, options: RequestOptions & { readonly schema?: ResponseSchema<unknown> } = {},
  ): Promise<unknown> {
    const { schema } = options
    const raw = await execute(path, options)
    return schema === undefined ? raw : schema.parse(raw)
  }

  return {
    apiRequest,
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
