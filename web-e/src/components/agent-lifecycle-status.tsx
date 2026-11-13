import { useTranslation } from '@/hooks/use-translation'
import { Button } from '@/components/ui/button'
import type { useAgentLifecycle } from '@/hooks/use-agent-lifecycle'
import { lifecycleBlockMessage, mcpWarningMessage, recordedStatus, startFailureMessage } from '@/lib/agent-lifecycle-policy'
import { runtimeLossSummary } from '@/lib/agent-runtime-loss'
import { relativeTime } from '@/lib/relative-time'

import { RequestErrorToast } from '@/components/request-error-toast'
export function AgentLifecycleStatus({ query, expired, upgrading }: {
  readonly query: ReturnType<typeof useAgentLifecycle>
  readonly expired: boolean
  readonly upgrading: boolean
}) {
  const { t: translate, owned, formatDate, formatNumber, locale } = useTranslation()
  const lifecycle = query.data
  const start = lifecycle?.start
  const paused = !query.online || query.isPaused
  const canStart = query.confirmed && lifecycle?.availability.can_start && !expired && !upgrading
  return (
    <section aria-label={translate("Agent lifecycle")} className="min-h-112 space-y-3 rounded-xl border border-border p-4 text-[1.0625rem] leading-normal md:min-h-96 md:text-sm [overflow-wrap:anywhere] [&_.text-muted-foreground]:text-[0.9375rem] md:[&_.text-muted-foreground]:text-sm">
      <h2 className="font-display text-lg font-semibold md:text-base">{translate("Agent lifecycle")}</h2>
      {paused ? <p role="status">{translate("You are offline. Lifecycle checks will resume when you reconnect. Start and integration retries stay unavailable until status is confirmed.")}</p> : query.isError ? <>
        <RequestErrorToast error={query.error} message="Lifecycle status is unavailable. Start and integration retries stay unavailable until you check again." />
        <Button variant="outline" className="h-auto min-h-11 whitespace-normal text-[1.0625rem] transition-none md:text-sm" disabled={query.isFetching}
          onClick={() => void query.refetch()}>{query.isFetching ? translate("Checking lifecycle...") : translate("Check lifecycle again")}</Button>
      </> : null}
      {!query.confirmed ? <p role="status" className="text-muted-foreground">{paused ? translate("Waiting for a connection.") : query.isError ? translate("A new status check is required.") : translate("Checking recorded lifecycle status...")}</p> : null}
      {lifecycle && !query.isError ? <>
        <div role="status" aria-live="polite" className="space-y-2">
          <p>{translate("Recorded runtime:")}{' '}<strong className="font-medium">{owned(recordedStatus[lifecycle.runtime.recorded_status])}</strong></p>
          {lifecycle.runtime.loss ? <>
            <p>{translate("Runtime loss:")}{' '}{translate(runtimeLossSummary[lifecycle.runtime.loss.reason])}</p>
            <p className="text-muted-foreground">{translate("Detected")}{' '}<time dateTime={lifecycle.runtime.loss.detected_at} title={formatDate(lifecycle.runtime.loss.detected_at, { dateStyle: 'medium', timeStyle: 'short' })}>{relativeTime(lifecycle.runtime.loss.detected_at, undefined, locale)}</time></p>
          </> : null}
          <p className="text-muted-foreground">{translate("Recorded state is not a live readiness check.")}</p>
          <p>{translate("Policy:")} {!query.confirmed ? translate("Unconfirmed") : expired || !lifecycle.availability.policy_eligible ? translate("Blocked") : translate("Eligible")}</p>
          {expired && !lifecycle.availability.blocking_codes.includes('expired') ? <p>{owned(lifecycleBlockMessage.expired)}</p> : null}
          {lifecycle.availability.blocking_codes.length ? <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
            {lifecycle.availability.blocking_codes.map((code) => <li key={code}>{owned(lifecycleBlockMessage[code])}</li>)}
          </ul> : null}
          {canStart && lifecycle.availability.blocking_codes.includes('runtime_stopped') ? <p>{translate("Use Start to request a start.")}</p> : null}
          <p className="text-muted-foreground">{translate("Recorded")}{' '}<time dateTime={lifecycle.as_of} title={formatDate(lifecycle.as_of, { dateStyle: 'medium', timeStyle: 'short' })}>{relativeTime(lifecycle.as_of, undefined, locale)}</time></p>
        </div>
        {start ? <div className="space-y-2" role="status" aria-live="polite">
          <h3 className="font-medium">{translate("Start request:")}{' '}{({ pending: translate("Pending"), succeeded: translate("Completed"), cancelled: translate("Cancelled") } as const)[start.state]}</h3>
        <p>{start.attempts === 1 ? translate("{count} attempt recorded", { count: formatNumber(start.attempts) }) : translate("{count} attempts recorded", { count: formatNumber(start.attempts) })}</p>
          {start.last_failure_code ? <p className="text-muted-foreground">{owned(startFailureMessage[start.last_failure_code])}</p> : null}
          {start.last_attempt_at ? <p>{translate("Last attempt:")}{' '}<time dateTime={start.last_attempt_at} title={formatDate(start.last_attempt_at, { dateStyle: 'medium', timeStyle: 'short' })}>{relativeTime(start.last_attempt_at, undefined, locale)}</time></p> : null}
          {start.next_retry_at ? <p>{translate("Next retry:")}{' '}<time dateTime={start.next_retry_at}>{formatDate(start.next_retry_at, { dateStyle: 'medium', timeStyle: 'short' })}</time> ({relativeTime(start.next_retry_at, undefined, locale)})</p>
            : start.state === 'pending' ? <p className="text-muted-foreground">{translate("No retry time is recorded. Check again or contact your company admin if it stays pending.")}</p> : null}
        </div> : null}
        <div className="space-y-3">
          <h3 className="font-medium">{translate("Optional integrations")}</h3>
          {!lifecycle.optional_mcp.length ? <p className="text-muted-foreground">{translate("No optional integration outcomes recorded.")}</p> : <ul className="space-y-3">
            {lifecycle.optional_mcp.map((item) => <li key={item.server_id} className="space-y-2">
              <div role="status" aria-live="polite" className="space-y-1">
                <p className="font-medium">{item.server_id}</p>
          <p>{({ pending: translate("Import queued"), running: translate("Import in progress"), succeeded: translate("Imported"), warning: translate("Needs attention") } as const)[item.state]} · {item.attempts === 1 ? translate("{count} attempt", { count: formatNumber(item.attempts) }) : translate("{count} attempts", { count: formatNumber(item.attempts) })}</p>
                <p className="min-h-[3lh] text-muted-foreground">{item.warning_code ? owned(mcpWarningMessage[item.warning_code]) : ({ pending: translate("Waiting for the integration import to begin."), running: translate("The integration import is in progress."), succeeded: translate("The integration was imported."), warning: translate("Check the saved integration connection.") } as const)[item.state]}</p>
              </div>
              <Button variant="outline" className="h-auto min-h-11 whitespace-normal text-[1.0625rem] transition-none md:text-sm"
                disabled={item.state !== 'warning' || !item.retryable || !query.confirmed || query.retry.isPending || query.isFetching || expired || upgrading || !lifecycle.availability.can_retry_mcp}
                aria-label={item.state === 'warning' ? translate("Retry integration {server}", { server: item.server_id }) : translate("{status} {server}", { status: ({ pending: translate("Integration queued"), running: translate("Importing integration"), succeeded: translate("Integration imported") } as const)[item.state], server: item.server_id })}
                onClick={() => query.retry.mutate(item.server_id)}>
                {item.state !== 'warning' ? ({ pending: translate("Integration queued"), running: translate("Importing integration"), succeeded: translate("Integration imported") } as const)[item.state] : translate("Retry integration")}
              </Button>
            </li>)}
          </ul>}
          <div className="min-h-[3lh]" role="status" aria-live="polite">
            {query.retry.isPaused ? <p>{translate("Integration retry is paused while offline. It will resume when you reconnect.")}</p> : query.retry.isPending ? <p>{translate("Requesting integration retry...")}</p> : query.retry.isError ? <RequestErrorToast error={query.retry.error} message="Unable to retry this integration. Check its saved connection and lifecycle status, then try again." /> : null}
          </div>
        </div>
      </> : null}
    </section>
  )
}
