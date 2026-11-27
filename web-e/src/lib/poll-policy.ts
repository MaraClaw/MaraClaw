import { jitteredBackoff } from './backoff.ts'
import { ApiError } from './http.ts'

export const POLL_CEILING_MS = 60_000

export type PollEnvironment = {
  readonly hidden: () => boolean
  readonly random: () => number
}

export const browserPollEnvironment: PollEnvironment = {
  hidden: () => typeof document !== 'undefined' && document.visibilityState === 'hidden',
  random: Math.random,
}

export type PollState = {
  readonly cadence: number
  readonly failures: number
  readonly retryAfterMs: number | null
}

// False while the page is hidden: the caller resumes when it becomes visible again.
export function nextPollDelay({ cadence, failures, retryAfterMs }: PollState, env: PollEnvironment = browserPollEnvironment): number | false {
  if (env.hidden()) return false
  if (failures === 0) return cadence
  const ceiling = Math.max(cadence, POLL_CEILING_MS)
  return Math.min(ceiling, Math.max(cadence, jitteredBackoff(failures, cadence, ceiling, env.random), retryAfterMs ?? 0))
}

type PolledQuery<TData> = {
  readonly state: {
    readonly data: TData | undefined
    readonly error: Error | null
    readonly dataUpdateCount: number
    readonly errorUpdateCount: number
  }
}

// TanStack restarts its own failure count with every fetch, so with `retry: false` it never passes 1. The streak is
// counted from the cumulative error count and rebased whenever a success lands.
const streaks = new WeakMap<object, { readonly successes: number; readonly errors: number }>()

function failureStreak(query: PolledQuery<unknown>): number {
  const { dataUpdateCount, errorUpdateCount } = query.state
  const seen = streaks.get(query)
  if (seen?.successes !== dataUpdateCount) {
    streaks.set(query, { successes: dataUpdateCount, errors: errorUpdateCount })
    return 0
  }
  return errorUpdateCount - seen.errors
}

// `false` while hidden leaves the interval stopped until the next state update, so the query must refetch when the
// page is visible again (`refetchOnWindowFocus` on a stale query, or 'always').
export function queryPollInterval<TData>(
  cadence: (data: TData | undefined) => number | false,
  env: PollEnvironment = browserPollEnvironment,
) {
  return (query: PolledQuery<TData>): number | false => {
    const failures = failureStreak(query)
    const healthy = cadence(query.state.data)
    if (healthy === false) return false
    const { error } = query.state
    return nextPollDelay({ cadence: healthy, failures, retryAfterMs: error instanceof ApiError ? error.retryAfterMs : null }, env)
  }
}
