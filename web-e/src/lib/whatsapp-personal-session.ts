import { ApiError } from './http.ts'
import type { PersonalCommand, PersonalStatus } from './whatsapp-api.ts'

export type PersonalView = {
  readonly status: PersonalStatus | null
  readonly loading: boolean
  readonly busy: boolean
  readonly error: string | null
}
type PersonalTransport = {
  readonly current: () => boolean
  readonly read: (signal: AbortSignal) => Promise<PersonalStatus>
  readonly change: (command: PersonalCommand, signal: AbortSignal) => Promise<PersonalStatus>
}
type PersonalClock = {
  readonly now: () => number
  readonly later: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  readonly cancel: (timer: ReturnType<typeof setTimeout>) => void
}
const browserClock: PersonalClock = { now: Date.now, later: (callback, delay) => setTimeout(callback, delay), cancel: (timer) => clearTimeout(timer) }

/** View-local secret lifetime. Never install this object or its snapshots into a query cache. */
export function createPersonalSession(transport: PersonalTransport, clock: PersonalClock = browserClock) {
  let view: PersonalView = { status: null, loading: true, busy: false, error: null }
  let active = false
  let epoch = 0
  let request: AbortController | undefined
  let poll: ReturnType<typeof setTimeout> | undefined
  let expiry: ReturnType<typeof setTimeout> | undefined
  const listeners = new Set<() => void>()
  const retiredGenerations = new Set<string>()
  const retiredAttempts = new Set<string>()
  const invalidatedQrAttempts = new Set<string>()
  let watermark: Pick<PersonalStatus, 'generation' | 'attempt_id' | 'revision'> | null = null
  let qrDeadline: number | null = null
  const publish = (next: PersonalView) => { view = next; for (const listener of listeners) listener() }
  const current = (token: number) => active && transport.current() && token === epoch
  function clearWork() {
    request?.abort()
    if (poll !== undefined) clock.cancel(poll)
    if (expiry !== undefined) clock.cancel(expiry)
    request = undefined; poll = undefined; expiry = undefined
  }
  function begin() { epoch += 1; clearWork(); return epoch }
  function accept(incoming: PersonalStatus, replace: boolean): boolean {
    const previous = watermark
    if (incoming.generation && retiredGenerations.has(incoming.generation)) return false
    if (incoming.attempt_id && retiredAttempts.has(incoming.attempt_id)) return false
    if (previous) {
      if (incoming.generation === previous.generation && incoming.revision < previous.revision) return false
      if (incoming.generation === previous.generation && incoming.revision === previous.revision && incoming.attempt_id !== previous.attempt_id) return false
      if (incoming.generation === previous.generation && incoming.revision === previous.revision && view.status) return false
      if (incoming.generation !== previous.generation && !replace) {
        // A concurrent tab replaced the connection. Retire the old identity before accepting it.
        if (incoming.generation === null) return false
      }
      if (previous.generation && incoming.generation !== previous.generation) retiredGenerations.add(previous.generation)
      if (previous.attempt_id && incoming.attempt_id !== previous.attempt_id) retiredAttempts.add(previous.attempt_id)
    }
    if (expiry !== undefined) clock.cancel(expiry)
    const deadline = Math.min(...[incoming.expires_at, incoming.qr?.expires_at].flatMap((value) => value ? [Date.parse(value)] : []))
    const pairing = incoming.state === 'qr' || incoming.state === 'pending'
    const expired = pairing && (deadline <= clock.now() || incoming.attempt_id !== null && invalidatedQrAttempts.has(incoming.attempt_id))
    const repeat = previous?.generation === incoming.generation && previous.revision === incoming.revision
    const status: PersonalStatus = {
      ...incoming, state: expired ? 'expired' : incoming.state,
      qr: incoming.state === 'qr' && !expired && (!repeat || deadline === qrDeadline) && incoming.generation !== null && incoming.attempt_id !== null ? incoming.qr : null,
    }
    qrDeadline = status.qr ? deadline : null
    if (status.state === 'expired' && status.attempt_id) invalidatedQrAttempts.add(status.attempt_id)
    watermark = { generation: incoming.generation, attempt_id: incoming.attempt_id, revision: incoming.revision }
    publish({ status, busy: false, loading: false, error: null })
    if (pairing && !expired && Number.isFinite(deadline)) {
      const token = epoch
      expiry = clock.later(() => {
        if (!current(token)) return
        const latest = view.status
        if (!latest) return
        if (latest.attempt_id) invalidatedQrAttempts.add(latest.attempt_id)
        qrDeadline = null
        begin() // Invalidate a held poll as well as removing the displayed secret.
        publish({ ...view, status: { ...latest, state: 'expired', qr: null }, busy: false })
      }, Math.max(0, deadline - clock.now()))
    }
    return true
  }
  function schedule(token: number) {
    if (!current(token) || view.error || !view.status) return
    switch (view.status.state) {
      case 'pending': case 'qr': case 'connected': case 'reconnecting': case 'paused': case 'disconnecting':
        poll = clock.later(() => { void read(token) }, 2000)
        return
      case 'expired': case 'disconnected': case 'failed': return
      default: return assertNever(view.status.state)
    }
  }
  function fail(error: unknown, token: number) {
    if (!current(token)) return
    if (!(error instanceof Error)) throw error
    clearWork()
    const message = error instanceof ApiError && error.status === 409
      ? 'Connection changed elsewhere. Refresh status before trying again.'
      : error instanceof ApiError && (error.status === 403 || error.status === 404)
        ? 'This connection is no longer available to you.'
        : 'Unable to update the connection. Check your network and refresh status.'
    publish({ status: null, loading: false, busy: false, error: message })
  }
  async function read(token: number) {
    if (!current(token)) return
    request = new AbortController()
    try {
      const status = await transport.read(AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]))
      if (!current(token)) return
      if (!accept(status, false) && !view.status) publish({ status: null, loading: false, busy: false, error: 'Connection status is outdated. Refresh status to check the current connection.' })
      schedule(token)
    } catch (error) { fail(error, token) }
  }
  async function change(command: PersonalCommand) {
    if (!active || !transport.current() || view.busy || view.loading || view.error) return
    const token = begin()
    if (view.status?.attempt_id) invalidatedQrAttempts.add(view.status.attempt_id)
    qrDeadline = null
    publish({ ...view, status: view.status ? { ...view.status, qr: null } : null, busy: true, error: null })
    request = new AbortController()
    try {
      const status = await transport.change(command, AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]))
      if (!current(token)) return
      if (!accept(status, true)) publish({ ...view, busy: false })
      schedule(token)
    } catch (error) { fail(error, token) }
  }
  return {
    getSnapshot: () => view,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    start() { active = true; const token = begin(); publish({ status: null, loading: true, busy: false, error: null }); void read(token) },
    refresh() { if (!active || !transport.current()) return; const token = begin(); publish({ status: null, loading: true, busy: false, error: null }); void read(token) },
    change,
    stop() { active = false; begin(); publish({ status: null, loading: true, busy: false, error: null }); watermark = null; qrDeadline = null; retiredGenerations.clear(); retiredAttempts.clear(); invalidatedQrAttempts.clear() },
  }
}

function assertNever(value: never): never { throw new TypeError(`Unknown personal state: ${typeof value}`) }
