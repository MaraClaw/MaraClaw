import { useTranslation } from '@/hooks/use-translation'
import { useInfiniteQuery, type QueryFunctionContext } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { listScheduleHistoryPage, scheduleHistoryOutcomeSchema, type ScheduleHistoryItem, type ScheduleHistoryOutcome } from '@/lib/schedules-api'

const outcomeLabels: Readonly<Record<ScheduleHistoryItem['status'], string>> = {
  success: 'Success', failure: 'Failure', cancelled: 'Cancelled', unknown: 'Unknown',
}

export function ScheduleRunHistory({ agentId, scheduleId }: {
  readonly agentId: string
  readonly scheduleId: string
}) {
  const { t: translate, owned } = useTranslation()
  const scope = useWorkspaceSession()
  const headingId = useId()
  const filterId = useId()
  const [outcome, setOutcome] = useState<ScheduleHistoryOutcome>('all')

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h3 id={headingId} className="text-sm font-semibold">{translate("Run history")}</h3>
      <div className="grid max-w-xs gap-2">
        <Label htmlFor={filterId}>{translate("Outcome")}</Label>
        <Select id={filterId} value={outcome} onChange={scope.effect((event) => setOutcome(scheduleHistoryOutcomeSchema.parse(event.currentTarget.value)))}>
          <option value="all">{translate("All outcomes")}</option>
          {Object.entries(outcomeLabels).map(([value, label]) => <option key={value} value={value}>{owned(label)}</option>)}
        </Select>
      </div>
      <HistoryPages key={outcome} agentId={agentId} scheduleId={scheduleId} outcome={outcome} />
    </section>
  )
}

function HistoryPages({ agentId, scheduleId, outcome }: {
  readonly agentId: string
  readonly scheduleId: string
  readonly outcome: ScheduleHistoryOutcome
}) {
  const { t: translate, owned, formatDate } = useTranslation()
  const scope = useWorkspaceSession()
  const instanceId = useId()
  const history = useInfiniteQuery({
    queryKey: ['schedule-history', agentId, scheduleId, outcome, instanceId],
    initialPageParam: undefined,
    queryFn: ({ pageParam, signal }: QueryFunctionContext<readonly string[], string | undefined>) =>
      scope.run(() => listScheduleHistoryPage(agentId, scheduleId, { cursor: pageParam, signal, outcome })),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    retry: false,
    gcTime: 0,
  }, scope.client)
  const rows = history.data?.pages.flatMap((page) => page.items) ?? []

  return (
    <>
      {history.isPending || (history.isFetching && !history.data) ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading run history...")}</p> : null}
      {history.isError && !history.isFetching ? <div className="space-y-2">
        <RequestErrorToast error={history.error} message={userFacingRequestError(history.error,
          history.isFetchNextPageError ? translate("Unable to load older runs. Try again.") : translate("Unable to load run history. Try again."))} />
        {!history.data ? <Button variant="outline" disabled={history.isFetching}
          onClick={scope.effect(() => { void history.refetch() })}>{translate("Retry history")}</Button> : null}
      </div> : null}
      {history.isSuccess && rows.length === 0 ? <p className="text-sm text-muted-foreground">{outcome === 'all' ? translate("No history yet.") : translate("No runs with this outcome.")}</p> : null}
      {rows.length > 0 ? <ul aria-label={translate("Schedule runs")} className="space-y-2">
        {rows.map((row) => <li key={row.id} className="space-y-2 rounded-xl border border-border p-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">{owned(outcomeLabels[row.status])}</Badge>
            {row.created_at ? <time dateTime={row.created_at}>{formatDate(row.created_at, { dateStyle: 'medium', timeStyle: 'short' })}</time> : <span>{translate("Time unavailable")}</span>}
          </div>
          <p className="whitespace-pre-wrap break-words text-sm">{row.summary || row.reply || row.instruction || translate("No output recorded.")}</p>
        </li>)}
      </ul> : null}
      {history.isFetchingNextPage ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading older runs...")}</p> : null}
      {history.hasNextPage ? <Button variant="outline" disabled={history.isFetching}
        onClick={scope.effect(() => { if (!history.isFetching) void history.fetchNextPage() })}>
        {history.isFetchingNextPage ? translate("Loading older runs...") : history.isFetchNextPageError ? translate("Retry older runs") : translate("Load older runs")}
      </Button> : null}
      {rows.length > 0 && !history.hasNextPage ? <p className="text-xs text-muted-foreground">{translate("No older runs.")}</p> : null}
    </>
  )
}
