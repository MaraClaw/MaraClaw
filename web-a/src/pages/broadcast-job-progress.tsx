import { useQuery } from '@tanstack/react-query'
import { useEffect, useId, useLayoutEffect, useRef } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { adminSession } from '@/lib/admin-session'
import { fetchBroadcastJobStatus, type BroadcastAcceptance, type BroadcastResult } from '@/lib/broadcast-api'
import { runOwned } from '@/lib/session-policy'

export function BroadcastJobProgress({ receipt, onCompleted }: {
  readonly receipt: BroadcastAcceptance
  readonly onCompleted: (result: BroadcastResult) => void
}) {
  const { t, number } = useTranslation()
  const id = useId()
  const { owner } = useAuth()
  const isCurrent = useSessionCurrent()
  const reported = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  useLayoutEffect(() => { heading.current?.focus() }, [])
  const status = useQuery({
    queryKey: ['admin-broadcast-job', receipt.broadcast_id, owner.generation],
    queryFn: ({ signal }) => runOwned(adminSession, owner, () => fetchBroadcastJobStatus(receipt.broadcast_id, signal)),
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (query) => query.state.status !== 'error' && query.state.data?.status !== 'completed' ? 2_000 : false,
  })
  const data = status.data

  useEffect(() => {
    if (data?.status !== 'completed' || reported.current || !isCurrent()) return
    reported.current = true
    onCompleted({ ok: true, broadcast_id: data.broadcast_id, users_notified: data.users_notified, agents_notified: data.agents_notified, emails_sent: data.emails_queued })
  }, [data, isCurrent, onCompleted])

  return (
    <section aria-labelledby={`${id}-heading`} className="admission-content flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <h2 ref={heading} tabIndex={-1} id={`${id}-heading`} className="font-display text-lg font-semibold focus-visible:outline-2 focus-visible:outline-ring">{t(data?.status === 'processing' ? 'Broadcast in progress' : 'Broadcast accepted')}</h2>
      <p className="text-sm">{t('Your broadcast is saved. Notifications continue processing even if a status update fails.')}</p>
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        {([
          ['Captured users', receipt.users_total], ['Captured agents', receipt.agents_total],
          ['Recipients processed', data?.processed ?? 0], ['Users notified', data?.users_notified ?? 0],
          ['Agents notified', data?.agents_notified ?? 0], ['Skipped targets', data?.skipped ?? 0],
          ['Queued email targets', data?.emails_queued ?? 0],
        ] as const).map(([label, count]) => <div key={label}><dt className="text-sm text-muted-foreground">{t(label)}</dt><dd className="mt-1 font-semibold tabular-nums">{number(count)}</dd></div>)}
      </dl>
      <p role="status" className="text-sm text-muted-foreground">{t(status.isFetching ? 'Checking broadcast progress...' : 'Email progress and completed history will be available when notification processing finishes.')}</p>
    {status.isError ? <ErrorToast error={status.error} message={t('Could not refresh broadcast progress. The accepted job is kept; counts may be out of date. Refresh progress to try again.')} /> : null}
      <Button type="button" variant="outline" className="h-auto min-h-11 self-start whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none" disabled={status.isFetching} onClick={() => { if (isCurrent()) void status.refetch() }}>{t('Refresh broadcast progress')}</Button>
    </section>
  )
}
