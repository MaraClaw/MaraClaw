import { useEffect, useMemo, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { cancelScheduleRun, getScheduleRun, type ScheduleRunReceipt as RunReceipt, type ScheduleRunTarget } from '@/lib/schedules-api'

const statusLabels: Readonly<Record<RunReceipt['status'], string>> = {
  pending: 'Pending', running: 'Running', success: 'Success', failure: 'Failure', cancelled: 'Cancelled', unknown: 'Unknown',
}

function isActive(status: RunReceipt['status']) {
  switch (status) {
    case 'pending': case 'running': return true
    case 'success': case 'failure': case 'cancelled': case 'unknown': return false
    default: return status satisfies never
  }
}

function isUnavailable(error: unknown) {
  return error instanceof ApiError && (error.status === 403 || error.status === 404)
}

/** Keyed by acceptance: no mutation callback may outlive this receipt. */
export function ScheduleRunReceipt({ agentId, scheduleId, runId }: ScheduleRunTarget) {
  const scope = useWorkspaceSession()
  const lifetime = useRef(new AbortController())
  const target = useMemo(() => ({ agentId, scheduleId, runId }), [agentId, scheduleId, runId])
  const queryKey = useMemo(() => ['schedule-run', agentId, scheduleId, runId] as const, [agentId, scheduleId, runId])
  const [cancelling, setCancelling] = useState(false)
  const [accessLost, setAccessLost] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller
    return () => { controller.abort() }
  }, [])

  const receipt = useQuery({
    queryKey,
    queryFn: ({ signal }) => getScheduleRun(target, { signal: AbortSignal.any([signal, lifetime.current.signal]) }),
    enabled: (query) => !cancelling && !accessLost && !isUnavailable(query.state.error)
      && (!query.state.data || isActive(query.state.data.status)),
    refetchInterval: (query) => {
      if (cancelling || accessLost || isUnavailable(query.state.error)) return false
      return !query.state.data || isActive(query.state.data.status) ? 2000 : false
    },
    retry: false, gcTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false,
  })
  const cancel = useMutation({
    onMutate: async (signal: AbortSignal) => {
      signal.throwIfAborted()
      setCancelling(true)
      setMessage(null)
      await scope.client.cancelQueries({ queryKey, exact: true })
      signal.throwIfAborted()
    },
    mutationFn: (signal: AbortSignal) => cancelScheduleRun(target, { signal }),
    onSuccess: async (result, signal) => {
      if (signal.aborted) return
      // Also fence a focus/manual refetch that may have started during cancellation.
      await scope.client.cancelQueries({ queryKey, exact: true })
      if (signal.aborted || !scope.isCurrent()) return
      scope.client.setQueryData(queryKey, result)
    },
    onError: async (error, signal) => {
      if (signal.aborted) return
      if (error instanceof ApiError && error.status === 409) {
        setMessage('Cancellation lost the race. This request did not stop the run.')
        await scope.client.cancelQueries({ queryKey, exact: true })
        if (signal.aborted || !scope.isCurrent()) return
        await receipt.refetch()
        return
      }
      if (isUnavailable(error)) setAccessLost(true)
      setMessage(userFacingRequestError(error, 'Cancellation could not be confirmed. Check the run status before trying again.'))
    },
    onSettled: (_result, _error, signal) => { if (!signal.aborted) setCancelling(false) },
  })
  function requestCancel() {
    if (scope.isCurrent() && !cancel.isPending) cancel.mutate(lifetime.current.signal)
  }

  return (
    <section aria-label="Current run" className="mt-2 space-y-2">
      <div aria-live="polite" className="flex flex-wrap items-center gap-2 text-sm">
        {receipt.data && !receipt.isError && !accessLost ? <>
          <Badge variant="outline">{statusLabels[receipt.data.status]}</Badge>
          {receipt.data.status === 'pending' ? <span className="text-muted-foreground">Waiting to start.</span> : null}
          {receipt.data.status === 'running' ? <span className="text-muted-foreground">Already running; cancellation is unavailable.</span> : null}
          {receipt.data.status === 'unknown' ? <span className="text-muted-foreground">The final outcome is unavailable. Check history for details.</span> : null}
        </> : <span className="text-muted-foreground">{receipt.isError || accessLost ? 'Run status unavailable.' : 'Loading run status…'}</span>}
      </div>
      <p className="break-all text-xs text-muted-foreground">Run <code>{runId}</code></p>
      {receipt.isError ? <p role="status" className="text-sm text-destructive">{userFacingRequestError(receipt.error, 'Unable to load run status. Try again.')}</p> : null}
      {message ? <p role="status" className="text-sm text-muted-foreground">{message}</p> : null}
      {receipt.data?.can_cancel && !accessLost && !isUnavailable(receipt.error) ? <Button size="sm" variant="outline" disabled={cancel.isPending}
        onClick={requestCancel}>
        {cancel.isPending ? 'Cancelling…' : 'Cancel pending run'}
      </Button> : null}
      {receipt.isError && !isUnavailable(receipt.error) ? <Button size="sm" variant="outline" disabled={receipt.isFetching || cancel.isPending}
        onClick={scope.effect(() => { void receipt.refetch() })}>Retry status</Button> : null}
    </section>
  )
}
