import { apiUrl } from './api.ts'
import { adminSession } from './admin-session.ts'
import type { SessionPolicy } from './session-policy.ts'
import type { MessageKey } from './translations.ts'

export const REQUEST_TIMEOUT_MESSAGE = 'The server took too long to respond. Check your connection and try again.' satisfies MessageKey

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
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(formatApiDetail(message) ?? formatApiDetail(detail) ?? (status >= 500
      ? 'The server is temporarily unavailable. Please try again shortly.'
      : `Request failed (${status})`))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function formatApiDetail(detail: unknown): string | null {
  if (detail == null) return null
  if (typeof detail === 'string') {
    const text = detail.trim()
    return text && text.length <= 500 && !/<[^>]*>|&lt;|&#0*60;|&#x0*3c;/iu.test(text) ? text : null
  }
  if (typeof detail === 'object') {
    const obj = detail as Record<string, unknown>
    if (typeof obj.message === 'string') return formatApiDetail(obj.message)
    if (typeof obj.detail === 'string') return formatApiDetail(obj.detail)
    if (Array.isArray(obj.detail)) {
      return formatApiDetail(obj.detail
        .map((item) => {
          if (typeof item === 'string') return formatApiDetail(item)
          if (item && typeof item === 'object' && 'msg' in item) {
            return formatApiDetail(item.msg)
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
  readonly signal?: AbortSignal
  readonly idempotencyKey?: string
  /** Replaces the default deadline; covers the response body as well as the headers. */
  readonly timeoutMs?: number
}

export type ResponseSchema<T> = {
  readonly parse: (input: unknown) => T
}

export type SchemaRequestOptions<T> = RequestOptions & {
  readonly schema: ResponseSchema<T>
}

type Transport = {
  readonly fetch: typeof globalThis.fetch
  readonly url: (path: string) => string
  /** The deadline signal for one request (default `AbortSignal.timeout`); tests inject one they abort themselves. */
  readonly deadline?: (timeoutMs: number) => AbortSignal
}

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
  constructor() { super('Files over 20 MiB cannot be previewed.') }
}

type Bytes = Uint8Array<ArrayBuffer>

async function readPreviewBytes(response: Response, assertActive: () => void, signal: AbortSignal): Promise<Bytes> {
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

async function execute(
  policy: SessionPolicy, transport: Transport, path: string, options: ExecutionOptions & { readonly binary: true },
): Promise<Bytes>
async function execute(
  policy: SessionPolicy, transport: Transport, path: string, options: ExecutionOptions,
): Promise<unknown>
async function execute(
  policy: SessionPolicy, transport: Transport, path: string, options: ExecutionOptions,
): Promise<unknown> {
  const headers: Record<string, string> = { Accept: options.binary ? 'application/octet-stream' : 'application/json' }
  const current = policy.capture()
  const token = options.token === undefined ? current.token : options.token
  const owner = token ? { ...current, token } : null
  if (owner) policy.assertCurrent(owner)
  // Binary bodies are capped at MAX_PREVIEW_BYTES, so their default deadline is sized for that payload.
  const timeoutMs = options.timeoutMs ?? (options.binary ? transferTimeoutMs(MAX_PREVIEW_BYTES) : DEFAULT_REQUEST_TIMEOUT_MS)
  const deadline = transport.deadline?.(timeoutMs) ?? AbortSignal.timeout(timeoutMs)
  const signal = AbortSignal.any([
    deadline, ...(owner ? [owner.signal] : []), ...(options.signal ? [options.signal] : []),
  ])
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.idempotencyKey !== undefined) headers['Idempotency-Key'] = options.idempotencyKey

  const { form } = options
  let body: BodyInit | undefined = form
  if (!form && options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  // Precedence: a retired session, then the caller's own abort, then the deadline.
  function assertActive() {
    if (owner) policy.assertCurrent(owner)
    options.signal?.throwIfAborted()
    if (deadline.aborted) throw new RequestTimeoutError(timeoutMs, deadline.reason)
  }

  try {
    assertActive()
    const response = await untilAborted(transport.fetch(transport.url(path), {
      method: options.method ?? (form || options.body !== undefined ? 'POST' : 'GET'),
      headers,
      body,
      signal,
      ...(options.binary ? { cache: 'no-store', redirect: 'error' } : {}),
    }), signal)
    assertActive()
    if (response.status === 204) return options.binary ? new Uint8Array() : undefined

    let text: string
    if (options.binary) {
      const bytes = await readPreviewBytes(response, assertActive, signal)
      assertActive()
      if (response.ok) return bytes
      text = new TextDecoder().decode(bytes)
    } else {
      text = await untilAborted(response.text(), signal)
    }
    assertActive()
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
    return data
  } catch (error) {
    // Expiry has already notified auth; preserve that request's ApiError.
    if (error instanceof ApiError && error.status === 401) throw error
    assertActive()
    throw error
  }
}

export function createApiRequest(policy: SessionPolicy, transport: Transport) {
  function apiRequest<T>(path: string, options: SchemaRequestOptions<T>): Promise<T>
  function apiRequest<T = unknown>(
    path: string,
    options?: RequestOptions & { readonly schema?: never },
  ): Promise<T>
  async function apiRequest<T>(
    path: string,
    options: RequestOptions & { readonly schema?: ResponseSchema<T> } = {},
  ): Promise<T> {
    const data = await execute(policy, transport, path, options)
    return options.schema ? options.schema.parse(data) : (data as T)
  }

  return apiRequest
}

const adminTransport: Transport = {
  fetch: (...args) => globalThis.fetch(...args),
  url: apiUrl,
}

/** Multipart upload: the browser sets the `multipart/form-data` boundary, so no JSON `Content-Type` is sent. */
export async function apiFormRequest<T>(
  path: string, form: FormData, options: Omit<RequestOptions, 'body'> = {},
): Promise<T> {
  return await execute(adminSession, adminTransport, path, { ...options, form }) as T
}

/** Authenticated bytes (no-store, no redirects, capped at {@link MAX_PREVIEW_BYTES}). */
export function apiBinaryRequest(path: string, options: Omit<RequestOptions, 'body'> = {}): Promise<Bytes> {
  return execute(adminSession, adminTransport, path, { ...options, binary: true })
}

export const apiRequest = createApiRequest(adminSession, adminTransport)
