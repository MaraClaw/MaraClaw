import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { ApiError } from './http.ts'
import { queryPollInterval } from './poll-policy.ts'
import { getScheduleRun, type ScheduleRunReceipt, type ScheduleRunTarget } from './schedules-api.ts'

export function scheduleRunKey({ agentId, scheduleId, runId }: ScheduleRunTarget) {
  return ['schedule-run', agentId, scheduleId, runId] as const
}

function isActiveRun(status: ScheduleRunReceipt['status']) {
  switch (status) {
    case 'pending': case 'running': return true
    case 'success': case 'failure': case 'cancelled': case 'unknown': return false
    default: return status satisfies never
  }
}

export function isRunUnavailable(error: unknown) {
  return error instanceof ApiError && (error.status === 403 || error.status === 404)
}

type ReceiptState = { readonly cancelling: boolean; readonly accessLost: boolean }

// An active receipt is read every 2 s until it settles; cancellation, lost access or a 403/404 stops it.
export function scheduleRunOptions(target: ScheduleRunTarget, { cancelling, accessLost }: ReceiptState) {
  const queryKey = scheduleRunKey(target)
  const queryFn = ({ signal }: QueryFunctionContext<typeof queryKey>) => getScheduleRun(target, { signal })
  const held = (error: Error | null) => cancelling || accessLost || isRunUnavailable(error)
  const poll = queryPollInterval((receipt: ScheduleRunReceipt | undefined) => !receipt || isActiveRun(receipt.status) ? 2000 : false)
  return { ...queryOptions({
    queryKey, queryFn,
    enabled: (query) => !held(query.state.error) && (!query.state.data || isActiveRun(query.state.data.status)),
    refetchInterval: (query) => held(query.state.error) ? false : poll(query),
    retry: false, gcTime: 0,
    // Enabled only while it polls, so focus refetches nothing but a poll that stopped while the page was hidden.
    refetchOnWindowFocus: 'always', refetchOnReconnect: false,
  }), queryFn }
}
