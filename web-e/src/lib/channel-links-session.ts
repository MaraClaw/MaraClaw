import type { ChannelLinkCode, ChannelLinkStatus } from './channel-links-api.ts'

type LinkView = {
  readonly status: ChannelLinkStatus | null
  readonly code: ChannelLinkCode | null
  readonly loading: boolean
  readonly busy: boolean
  readonly expired: boolean
  readonly error: string | null
}
type LinkTransport = {
  readonly current: () => boolean
  readonly read: (signal: AbortSignal) => Promise<ChannelLinkStatus>
  readonly issue: (signal: AbortSignal) => Promise<ChannelLinkCode>
  readonly unlink: (signal: AbortSignal) => Promise<void>
}

/** Codes and external identity hints exist only for this mounted owner and agent. */
export function createChannelLinkSession(transport: LinkTransport) {
  let view: LinkView = { status: null, code: null, loading: true, busy: false, expired: false, error: null }
  let active = false
  let epoch = 0
  let request: AbortController | undefined
  let poll: ReturnType<typeof setTimeout> | undefined
  let expiry: ReturnType<typeof setTimeout> | undefined
  const listeners = new Set<() => void>()
  const publish = (next: LinkView) => { view = next; for (const listener of listeners) listener() }
  const current = (token: number) => active && transport.current() && token === epoch
  function cancel() {
    request?.abort()
    if (poll !== undefined) clearTimeout(poll)
    if (expiry !== undefined) clearTimeout(expiry)
    request = undefined; poll = undefined; expiry = undefined
  }
  function begin() { epoch += 1; cancel(); return epoch }
  function signal() { request = new AbortController(); return AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]) }
  function fail(error: unknown, token: number) {
    if (!current(token)) return
    if (!(error instanceof Error)) throw error
    cancel()
    publish({ status: null, code: null, loading: false, busy: false, expired: false, error: 'Unable to update your account link. Refresh status and try again.' })
  }
  async function read(token: number) {
    try {
      const status = await transport.read(signal())
      if (!current(token)) return
      const code = status.available && !status.linked ? view.code : null
      publish({ ...view, status, code, loading: false, busy: false, error: null })
      if (code) poll = setTimeout(() => { void read(token) }, 2000)
      else if (expiry !== undefined) { clearTimeout(expiry); expiry = undefined }
    } catch (error) { fail(error, token) }
  }
  async function issue() {
    if (!active || !transport.current() || view.loading || view.busy || !view.status?.available) return
    const token = begin()
    publish({ ...view, code: null, busy: true, expired: false, error: null })
    try {
      const code = await transport.issue(signal())
      if (!current(token)) return
      const deadline = Date.parse(code.expires_at)
      if (deadline <= Date.now()) { publish({ ...view, code: null, busy: false, expired: true }); return }
      publish({ ...view, code, busy: false })
      expiry = setTimeout(() => {
        if (!current(token)) return
        begin()
        publish({ ...view, code: null, busy: false, expired: true })
      }, deadline - Date.now())
      poll = setTimeout(() => { void read(token) }, 2000)
    } catch (error) { fail(error, token) }
  }
  async function unlink() {
    if (!active || !transport.current() || view.loading || view.busy || !view.status?.linked) return
    const token = begin()
    publish({ ...view, code: null, busy: true, expired: false, error: null })
    try {
      await transport.unlink(signal())
      if (!current(token)) return
      await read(token)
    } catch (error) { fail(error, token) }
  }
  function refresh() {
    if (!active || !transport.current()) return
    const token = begin()
    publish({ status: null, code: null, loading: true, busy: false, expired: false, error: null })
    void read(token)
  }
  return {
    getSnapshot: () => view,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    start() { active = true; refresh() },
    refresh, issue, unlink,
    stop() { active = false; begin(); publish({ status: null, code: null, loading: true, busy: false, expired: false, error: null }) },
  }
}
