import { useInfiniteQuery, type QueryFunctionContext } from '@tanstack/react-query'
import { useId } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { listScheduleHistoryPage, type ScheduleHistoryItem } from '@/lib/schedules-api'

const outcomeLabels: Readonly<Record<ScheduleHistoryItem['status'], string>> = {
  success: 'Success', failure: 'Failure', cancelled: 'Cancelled', unknown: 'Unknown',
}

export function ScheduleRunHistory({ agentId, scheduleId }: {
  readonly agentId: string
  readonly scheduleId: string
}) {
  const scope = useWorkspaceSession()
  const instanceId = useId()
  const headingId = useId()
  const history = useInfiniteQuery({
    queryKey: ['schedule-history', agentId, scheduleId, instanceId],
    initialPageParam: undefined,
    queryFn: ({ pageParam, signal }: QueryFunctionContext<readonly string[], string | undefined>) =>
      scope.run(() => listScheduleHistoryPage(agentId, scheduleId, { cursor: pageParam, signal })),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    retry: false,
    gcTime: 0,
  }, scope.client)
  const rows = history.data?.pages.flatMap((page) => page.items) ?? []

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h3 id={headingId} className="text-sm font-semibold">Run history</h3>
      {history.isPending || (history.isFetching && !history.data) ? <p role="status" className="text-sm text-muted-foreground">Loading run history...</p> : null}
      {history.isError && !history.isFetching ? <div className="space-y-2">
        <p role="status" className="text-sm text-destructive">{userFacingRequestError(history.error,
          history.isFetchNextPageError ? 'Unable to load older runs. Try again.' : 'Unable to load run history. Try again.')}</p>
        {!history.data ? <Button variant="outline" disabled={history.isFetching}
          onClick={scope.effect(() => { void history.refetch() })}>Retry history</Button> : null}
      </div> : null}
      {history.isSuccess && rows.length === 0 ? <p className="text-sm text-muted-foreground">No history yet.</p> : null}
      {rows.length > 0 ? <ul aria-label="Schedule runs" className="space-y-2">
        {rows.map((row) => <li key={row.id} className="space-y-2 rounded-xl border border-border p-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">{outcomeLabels[row.status]}</Badge>
            {row.created_at ? <time dateTime={row.created_at}>{new Date(row.created_at).toLocaleString()}</time> : <span>Time unavailable</span>}
          </div>
          <p className="whitespace-pre-wrap break-words text-sm">{row.summary || row.reply || row.instruction || 'No output recorded.'}</p>
        </li>)}
      </ul> : null}
      {history.isFetchingNextPage ? <p role="status" className="text-sm text-muted-foreground">Loading older runs...</p> : null}
      {history.hasNextPage ? <Button variant="outline" disabled={history.isFetching}
        onClick={scope.effect(() => { if (!history.isFetching) void history.fetchNextPage() })}>
        {history.isFetchingNextPage ? 'Loading older runs...' : history.isFetchNextPageError ? 'Retry older runs' : 'Load older runs'}
      </Button> : null}
      {rows.length > 0 && !history.hasNextPage ? <p className="text-xs text-muted-foreground">No older runs.</p> : null}
    </section>
  )
}
