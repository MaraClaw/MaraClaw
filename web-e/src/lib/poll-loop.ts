import { ApiError } from './http.ts'
import { browserPollEnvironment, nextPollDelay, type PollEnvironment } from './poll-policy.ts'

export type PollLoopEnvironment = PollEnvironment & {
  readonly schedule: (callback: () => void, delay: number) => () => void
  readonly onVisible: (listener: () => void) => () => void
}

const browserLoopEnvironment: PollLoopEnvironment = {
  ...browserPollEnvironment,
  schedule: (callback, delay) => {
    const id = setTimeout(callback, delay)
    return () => clearTimeout(id)
  },
  onVisible: (listener) => {
    window.addEventListener('visibilitychange', listener)
    return () => window.removeEventListener('visibilitychange', listener)
  },
}

export type PollLoop = {
  readonly cadence: number
  // One request; a rejection is a failed request (its ApiError may carry Retry-After) and the tick reports it itself.
  readonly tick: () => Promise<void>
  readonly immediate?: boolean
}

// Polls outside TanStack by the shared policy: one request at a time, backing off after failures, idle while the page
// is hidden and resumed at once when it is visible again. Returns the stop function.
export function startPollLoop({ cadence, tick, immediate = false }: PollLoop, env: PollLoopEnvironment = browserLoopEnvironment): () => void {
  let failures = 0
  let retryAfterMs: number | null = null
  let cancelTimer: (() => void) | undefined
  let running = false
  let stopped = false

  function arm() {
    const delay = nextPollDelay({ cadence, failures, retryAfterMs }, env)
    if (delay !== false) cancelTimer = env.schedule(() => void run(), delay)
  }

  async function run() {
    cancelTimer = undefined
    if (env.hidden()) return
    running = true
    try {
      await tick()
      failures = 0
      retryAfterMs = null
    } catch (error) {
      failures += 1
      retryAfterMs = error instanceof ApiError ? error.retryAfterMs : null
    } finally {
      running = false
    }
    if (!stopped) arm()
  }

  const unwatch = env.onVisible(() => {
    if (!stopped && !running && cancelTimer === undefined && !env.hidden()) void run()
  })
  if (immediate) void run()
  else arm()

  return () => {
    stopped = true
    cancelTimer?.()
    cancelTimer = undefined
    unwatch()
  }
}
