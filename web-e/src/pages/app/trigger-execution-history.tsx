import { useTranslation } from '@/hooks/use-translation'
import { useInfiniteQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import type { TriggerExecution, TriggerExecutionTarget } from '@/lib/trigger-executions-api'
import { triggerExecutionQuery } from '@/lib/trigger-executions-query'

export function TriggerExecutionHistory({ target, name }: {
  readonly target: TriggerExecutionTarget
  readonly name: string
}) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const regionId = useId()
  const [expanded, setExpanded] = useState(false)

  if (!scope.isCurrent()) return null
  return <div className="mt-2 space-y-3">
    <Button type="button" variant="outline" className="h-auto min-h-11 max-w-full whitespace-normal text-left"
      aria-label={expanded ? translate('Hide execution history for {name}', { name }) : translate('Show execution history for {name}', { name })}
      aria-expanded={expanded} aria-controls={regionId}
      onClick={scope.effect(() => setExpanded((current) => !current))}>
      {expanded ? translate("Hide execution history") : translate("Execution history")}
    </Button>
    <section id={regionId} aria-label={translate('{name} execution history', { name })} hidden={!expanded}>
      {expanded ? <ExecutionPages key={`${target.agentId}:${target.triggerId}`} target={target} /> : null}
    </section>
  </div>
}

function ExecutionPages({ target }: { readonly target: TriggerExecutionTarget }) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const instanceId = useId()
  const history = useInfiniteQuery(triggerExecutionQuery(scope, target, instanceId), scope.client)
  const rows = history.data?.pages.flatMap((page) => page.items) ?? []

  if (!scope.isCurrent()) return null
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-display text-base font-semibold">{translate("Execution history")}</h3>
      {history.data ? <Button type="button" variant="ghost" disabled={history.isFetching}
        onClick={scope.effect(() => { void history.refetch() })}>{translate("Refresh history")}</Button> : null}
    </div>
    {history.isPending || (history.isFetching && !history.data)
      ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading execution history…")}</p> : null}
    {history.isRefetching && !history.isFetchingNextPage
      ? <p role="status" className="text-sm text-muted-foreground">{translate("Refreshing execution history…")}</p> : null}
    {history.isError && !history.isFetchNextPageError && !history.isFetching ? <div className="space-y-2">
      <RequestErrorToast error={history.error} message="Unable to load execution history. Try again." />
      {!history.data ? <Button type="button" variant="outline" onClick={scope.effect(() => { void history.refetch() })}>
        {translate("Retry history")}</Button> : null}
    </div> : null}
    {history.isSuccess && rows.length === 0
      ? <p role="status" className="text-sm text-muted-foreground">{translate("No executions yet.")}</p> : null}
    {rows.length > 0 ? <ul aria-label={translate("Trigger executions")} className="space-y-2">
      {rows.map((row) => <ExecutionRow key={row.id} row={row} />)}
    </ul> : null}
    {history.isFetchingNextPage
      ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading older executions…")}</p> : null}
    {history.hasNextPage ? <div className="space-y-2">
      {history.isFetchNextPageError && !history.isFetching
        ? <RequestErrorToast error={history.error} message="Unable to load older executions. Try again." /> : null}
      <Button type="button" variant="outline" disabled={history.isFetching}
        onClick={scope.effect(() => { if (!history.isFetching) void history.fetchNextPage() })}>
        {history.isFetchingNextPage ? translate("Loading older executions…") : history.isFetchNextPageError ? translate("Retry older executions") : translate("Load older executions")}
      </Button>
    </div> : null}
    {rows.length > 0 && !history.hasNextPage ? <p className="text-xs text-muted-foreground">{translate("No older executions.")}</p> : null}
  </div>
}

function ExecutionRow({ row }: { readonly row: TriggerExecution }) {
  const { t: translate, owned } = useTranslation()
  return <li className="min-w-0 space-y-2 rounded-xl border border-border p-3">
    <Badge variant="outline" className="max-w-full whitespace-normal wrap-anywhere">
      {owned(row.status.replace(/[_-]+/g, ' ').replace(/(^|\s)\S/g, (letter) => letter.toLocaleUpperCase()))}
    </Badge>
    <p className="text-sm text-muted-foreground">{translate("Scheduled")}{' '}<ExecutionTime value={row.scheduled_at} /></p>
    <details className="text-sm">
      <summary className="min-h-11 cursor-pointer rounded-lg py-3 text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">{translate("Execution details")}</summary>
      <dl className="grid min-w-0 gap-2 pt-2 select-text">
        {[
          [translate("Execution ID"), row.id], [translate("Trigger ID"), row.trigger_id], [translate("Agent ID"), row.agent_id], [translate("Source"), owned(row.source)], [translate("Status"), owned(row.status)],
        ].map(([label, value]) => <div key={label} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{label}</dt><dd className="wrap-anywhere">{value}</dd>
        </div>)}
        {[
          [translate("Created"), row.created_at], [translate("Started"), row.started_at], [translate("Finished"), row.finished_at],
        ].map(([label, value]) => <div key={label}>
          <dt className="text-xs text-muted-foreground">{label}</dt><dd><ExecutionTime value={value ?? null} /></dd>
        </div>)}
      </dl>
    </details>
  </li>
}

function ExecutionTime({ value }: { readonly value: string | null }) {
  const { t: translate, formatDate } = useTranslation()
  return value === null ? <>{translate("Not recorded")}</>
    : <time dateTime={value}>{formatDate(value, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })}</time>
}
