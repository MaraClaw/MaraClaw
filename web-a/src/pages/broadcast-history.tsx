import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { adminSession } from '@/lib/admin-session'
import { fetchBroadcastHistory } from '@/lib/broadcast-api'
import { ApiError } from '@/lib/http'
import { runOwned } from '@/lib/session-policy'
import { canSendBroadcast } from '@/lib/types/auth'

const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function BroadcastHistory() {
  const id = useId()
  const { user, owner, mustChangePassword } = useAuth()
  const isCurrent = useSessionCurrent()
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null])
  const [revision, setRevision] = useState(0)
  const cursor = cursors.at(-1) ?? null
  const history = useQuery({
    queryKey: ['admin-broadcast-history', user?.tenant_id, owner.generation, revision, cursor],
    queryFn: ({ signal }) => runOwned(adminSession, owner, () => fetchBroadcastHistory(cursor, signal)),
    enabled: canSendBroadcast(user) && Boolean(user?.tenant_id) && !mustChangePassword,
    gcTime: 0,
    retry: false,
  })
  const data = history.data

  return (
    <Card role="region" aria-labelledby={`${id}-heading`} className="admission-content min-w-0">
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id={`${id}-heading`} className="font-display text-lg font-semibold">Published broadcasts</h2>
          <Button type="button" variant="outline" className={actionClass} disabled={history.isFetching} onClick={() => {
            if (!isCurrent()) return
            setCursors([null])
            setRevision((value) => value + 1)
          }}>Refresh history</Button>
        </div>
        <p className="text-sm text-muted-foreground">Messages published by all administrators in your current organization, newest first. Email counts are targets queued at publication, not confirmed deliveries.</p>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-5" aria-busy={history.isFetching}>
        <p role="status" aria-live="polite" className="text-sm text-muted-foreground">{history.isFetching ? 'Loading published broadcasts...' : `Page ${cursors.length}`}</p>
        {history.isError ? <div className="flex flex-wrap items-center gap-3">
          <p role="alert" className="text-sm text-destructive">Could not load published broadcasts. {history.error instanceof ApiError ? history.error.message : 'Check your connection and try again.'}</p>
          <Button type="button" variant="outline" className={actionClass} disabled={history.isFetching} onClick={() => { if (isCurrent()) void history.refetch() }}>Retry history</Button>
        </div> : null}
        {history.isSuccess && history.data.items.length === 0 ? <div className="flex flex-col gap-2 text-sm">
          <p>{cursors.length > 1 ? 'No more published broadcasts.' : 'No published broadcasts yet.'}</p>
          <p className="text-muted-foreground">{cursors.length > 1 ? 'Use Back to return to the previous page.' : 'History records broadcasts published after history tracking was enabled. Publish a message above to start this history.'}</p>
        </div> : null}
        {data && data.items.length > 0 ? <ol aria-label="Published messages" className="flex min-w-0 flex-col divide-y divide-border">
          {data.items.map((item) => <li key={item.broadcast_id} className="flex min-w-0 flex-col gap-3 py-5 first:pt-0 last:pb-0">
            <h3 className="font-display text-lg font-semibold whitespace-pre-wrap">{item.title}</h3>
            <p className="whitespace-pre-wrap text-sm leading-relaxed">{item.body || 'No body'}</p>
            <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm text-muted-foreground">
              <p>Published by <span className="text-foreground">{item.sender_name}</span></p>
              <time dateTime={item.published_at}>{timeFormat.format(new Date(item.published_at))}</time>
            </div>
            <dl className="grid gap-3 text-sm sm:grid-cols-3">
              {[
                ['Users notified', item.users_notified], ['Agents notified', item.agents_notified], ['Email targets queued', item.emails_sent],
              ].map(([label, count]) => <div key={label}><dt className="text-muted-foreground">{label}</dt><dd className="font-medium tabular-nums">{count.toLocaleString()}</dd></div>)}
            </dl>
          </li>)}
        </ol> : null}
        <nav aria-label="Published broadcast pages" className="flex flex-wrap justify-between gap-3">
          <Button type="button" variant="outline" className={actionClass} disabled={cursors.length === 1 || history.isFetching} onClick={() => { if (isCurrent()) setCursors((value) => value.slice(0, -1)) }}>Back</Button>
          <Button type="button" variant="outline" className={actionClass} disabled={!data?.next_cursor || history.isFetching || history.isError} onClick={() => {
            if (isCurrent() && data?.next_cursor) setCursors((value) => [...value, data.next_cursor])
          }}>Older</Button>
        </nav>
      </CardContent>
    </Card>
  )
}
