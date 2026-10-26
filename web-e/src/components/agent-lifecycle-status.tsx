import { Button } from '@/components/ui/button'
import type { useAgentLifecycle } from '@/hooks/use-agent-lifecycle'
import { lifecycleBlockMessage, mcpWarningMessage, recordedStatus, startFailureMessage } from '@/lib/agent-lifecycle-policy'
import { relativeTime } from '@/lib/relative-time'

export function AgentLifecycleStatus({ query, expired, upgrading }: {
  readonly query: ReturnType<typeof useAgentLifecycle>
  readonly expired: boolean
  readonly upgrading: boolean
}) {
  const lifecycle = query.data
  const start = lifecycle?.start
  const paused = !query.online || query.isPaused
  const canStart = query.confirmed && lifecycle?.availability.can_start && !expired && !upgrading
  return (
    <section aria-label="Agent lifecycle" className="min-h-112 space-y-3 rounded-xl border border-border p-4 text-[1.0625rem] leading-normal md:min-h-96 md:text-sm [overflow-wrap:anywhere] [&_.text-muted-foreground]:text-[0.9375rem] md:[&_.text-muted-foreground]:text-sm">
      <h2 className="font-display text-lg font-semibold md:text-base">Agent lifecycle</h2>
      {paused ? <p role="status">You are offline. Lifecycle checks will resume when you reconnect. Start and integration retries stay unavailable until status is confirmed.</p> : query.isError ? <>
        <p role="alert" className="text-destructive">Lifecycle status is unavailable. Start and integration retries stay unavailable until you check again.</p>
        <Button variant="outline" className="h-auto min-h-11 whitespace-normal text-[1.0625rem] transition-none md:text-sm" disabled={query.isFetching}
          onClick={() => void query.refetch()}>{query.isFetching ? 'Checking lifecycle...' : 'Check lifecycle again'}</Button>
      </> : null}
      {!query.confirmed ? <p role="status" className="text-muted-foreground">{paused ? 'Waiting for a connection.' : query.isError ? 'A new status check is required.' : 'Checking recorded lifecycle status...'}</p> : null}
      {lifecycle && !query.isError ? <>
        <div role="status" aria-live="polite" className="space-y-2">
          <p>Recorded runtime: <strong className="font-medium">{recordedStatus[lifecycle.runtime.recorded_status]}</strong></p>
          <p className="text-muted-foreground">Recorded state is not a live readiness check.</p>
          <p>Policy: {!query.confirmed ? 'Unconfirmed' : expired || !lifecycle.availability.policy_eligible ? 'Blocked' : 'Eligible'}</p>
          {expired && !lifecycle.availability.blocking_codes.includes('expired') ? <p>{lifecycleBlockMessage.expired}</p> : null}
          {lifecycle.availability.blocking_codes.length ? <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
            {lifecycle.availability.blocking_codes.map((code) => <li key={code}>{lifecycleBlockMessage[code]}</li>)}
          </ul> : null}
          {canStart && lifecycle.availability.blocking_codes.includes('runtime_stopped') ? <p>Use Start to request a start.</p> : null}
          <p className="text-muted-foreground">Recorded <time dateTime={lifecycle.as_of} title={new Date(lifecycle.as_of).toLocaleString()}>{relativeTime(lifecycle.as_of)}</time></p>
        </div>
        {start ? <div className="space-y-2" role="status" aria-live="polite">
          <h3 className="font-medium">Start request: {({ pending: 'Pending', succeeded: 'Completed', cancelled: 'Cancelled' } as const)[start.state]}</h3>
          <p>{start.attempts} {start.attempts === 1 ? 'attempt' : 'attempts'} recorded</p>
          {start.last_failure_code ? <p className="text-muted-foreground">{startFailureMessage[start.last_failure_code]}</p> : null}
          {start.last_attempt_at ? <p>Last attempt: <time dateTime={start.last_attempt_at} title={new Date(start.last_attempt_at).toLocaleString()}>{relativeTime(start.last_attempt_at)}</time></p> : null}
          {start.next_retry_at ? <p>Next retry: <time dateTime={start.next_retry_at}>{new Date(start.next_retry_at).toLocaleString()}</time> ({relativeTime(start.next_retry_at)})</p>
            : start.state === 'pending' ? <p className="text-muted-foreground">No retry time is recorded. Check again or contact your company admin if it stays pending.</p> : null}
        </div> : null}
        <div className="space-y-3">
          <h3 className="font-medium">Optional integrations</h3>
          {!lifecycle.optional_mcp.length ? <p className="text-muted-foreground">No optional integration outcomes recorded.</p> : <ul className="space-y-3">
            {lifecycle.optional_mcp.map((item) => <li key={item.server_id} className="space-y-2">
              <div role="status" aria-live="polite" className="space-y-1">
                <p className="font-medium">{item.server_id}</p>
                <p>{({ pending: 'Import queued', running: 'Import in progress', succeeded: 'Imported', warning: 'Needs attention' } as const)[item.state]} · {item.attempts} {item.attempts === 1 ? 'attempt' : 'attempts'}</p>
                <p className="min-h-[3lh] text-muted-foreground">{item.warning_code ? mcpWarningMessage[item.warning_code] : ({ pending: 'Waiting for the integration import to begin.', running: 'The integration import is in progress.', succeeded: 'The integration was imported.', warning: 'Check the saved integration connection.' } as const)[item.state]}</p>
              </div>
              <Button variant="outline" className="h-auto min-h-11 whitespace-normal text-[1.0625rem] transition-none md:text-sm"
                disabled={item.state !== 'warning' || !item.retryable || !query.confirmed || query.retry.isPending || query.isFetching || expired || upgrading || !lifecycle.availability.can_retry_mcp}
                aria-label={item.state === 'warning' ? `Retry integration ${item.server_id}` : `${({ pending: 'Integration queued', running: 'Importing integration', succeeded: 'Integration imported' } as const)[item.state]} ${item.server_id}`}
                onClick={() => query.retry.mutate(item.server_id)}>
                {item.state !== 'warning' ? ({ pending: 'Integration queued', running: 'Importing integration', succeeded: 'Integration imported' } as const)[item.state] : 'Retry integration'}
              </Button>
            </li>)}
          </ul>}
          <div className="min-h-[3lh]" role="status" aria-live="polite">
            {query.retry.isPaused ? <p>Integration retry is paused while offline. It will resume when you reconnect.</p> : query.retry.isPending ? <p>Requesting integration retry...</p> : query.retry.isError ? <p role="alert" className="text-destructive">Unable to retry this integration. Check its saved connection and lifecycle status, then try again.</p> : null}
          </div>
        </div>
      </> : null}
    </section>
  )
}
