import assert from 'node:assert/strict'
import type { TestContext } from 'node:test'
import type { Query, QueryClient, QueryFunction, QueryKey } from '@tanstack/react-query'

// Node has no document, so polling sees a visible page unless a test installs one; it is restored afterwards.
export function pageVisibility(t: TestContext, initial: 'visible' | 'hidden') {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const page = { visibilityState: initial }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: page })
  t.after(() => {
    if (original) Object.defineProperty(globalThis, 'document', original)
    else Reflect.deleteProperty(globalThis, 'document')
  })
  return { hide: () => { page.visibilityState = 'hidden' }, show: () => { page.visibilityState = 'visible' } }
}

type PolledOptions<TData, TKey extends QueryKey> = {
  readonly queryKey: NoInfer<TKey>
  readonly queryFn: QueryFunction<TData, TKey>
  readonly refetchInterval?: number | false | ((query: Query<TData, Error, TData, TKey>) => number | false | undefined) | undefined
}

// What an observer would schedule next for the query behind these options, evaluated as TanStack does after each update.
export function pollInterval<TData, TKey extends QueryKey>(client: QueryClient, options: PolledOptions<TData, TKey>) {
  const { refetchInterval } = options
  return typeof refetchInterval === 'function'
    ? refetchInterval(client.getQueryCache().build<TData, Error, TData, TKey>(client, options))
    : refetchInterval
}

export const throttled = (status: 429 | 503, retryAfter?: string) => Response.json({ detail: 'Busy' }, {
  status, headers: retryAfter === undefined ? {} : { 'Retry-After': retryAfter },
})

// Jitter keeps a backed-off delay between half of its ceiling and the ceiling.
export function assertBackedOff(delay: number | false | undefined, ceiling: number) {
  assert.equal(typeof delay, 'number')
  assert.ok(typeof delay === 'number' && delay >= ceiling / 2 && delay <= ceiling, `${String(delay)} is not within [${ceiling / 2}, ${ceiling}]`)
}
