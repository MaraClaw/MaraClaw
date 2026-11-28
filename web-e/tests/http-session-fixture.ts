import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createHttpClient } from '../src/lib/http.ts'
import type { RequestOptions } from '../src/lib/http.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

export const user: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}

/** A store signed in as `token-a` and a client over `fetchRequest`. */
export function httpFixture(fetchRequest: typeof fetch) {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({
    read: () => token,
    write: (value) => { token = value },
    clear: () => { token = null },
  })
  store.applySession(store.capture(), { access_token: 'token-a', user })
  // Every request shares one injected deadline, which a test expires by hand instead of waiting for it.
  const deadline = new AbortController()
  const deadlines: number[] = []
  const client = createHttpClient(store, {
    fetch: fetchRequest,
    url: (path) => `https://api.example${path}`,
    deadline: (timeoutMs) => { deadlines.push(timeoutMs); return deadline.signal },
  })
  const timeOut = () => { deadline.abort(new DOMException('signal timed out', 'TimeoutError')) }
  return { store, client, stored: () => token, deadlines, timeOut }
}

/** Sends `/resource` as JSON or as a multipart form. */
export function requestAs(kind: 'JSON' | 'multipart') {
  return (client: ReturnType<typeof createHttpClient>, options: RequestOptions = {}) => kind === 'JSON'
    ? client.apiRequest('/resource', options)
    : client.apiFormRequest('/resource', new FormData(), options)
}

/** A transport that never answers, as behind a proxy holding the connection open. */
export const hang = () => new Promise<Response>(() => undefined)

export class DeferredBodyResponse extends Response {
  readonly started = Promise.withResolvers<void>()
  readonly bodyResult = Promise.withResolvers<string>()
  override text(): Promise<string> {
    this.started.resolve()
    return this.bodyResult.promise
  }
}
