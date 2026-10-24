import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useLayoutEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { broadcastEmailStatusOptions } from '@/hooks/broadcast-options'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { cancelBroadcastEmail, type BroadcastResult } from '@/lib/broadcast-api'

const buttonMotion = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function BroadcastEmailProgress({ receipt }: { readonly receipt: BroadcastResult }) {
  const id = useId()
  const client = useQueryClient()
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const submitting = useRef(false)
  const progressHeading = useRef<HTMLHeadingElement>(null)
  const confirmationHeading = useRef<HTMLHeadingElement>(null)
  const feedback = useRef<HTMLDivElement>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const options = broadcastEmailStatusOptions(receipt.broadcast_id)
  const status = useQuery({ ...options, enabled: receipt.emails_sent > 0, refetchInterval: busy ? false : options.refetchInterval })
  const data = status.data

  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const cancellation = useSessionMutation({
    mutationFn: cancelBroadcastEmail,
    retry: false,
    onMutate: () => client.cancelQueries({ queryKey: options.queryKey }),
    onSuccess: (result) => {
      if (!active.current || !isSessionCurrent()) return
      client.setQueryData(options.queryKey, result)
    },
    onSettled: async () => {
      if (!active.current || !isSessionCurrent()) return
      setConfirming(false)
      // Both an ambiguous failure and a successful cancellation can race a worker.
      await status.refetch()
      if (!active.current || !isSessionCurrent()) return
      submitting.current = false
      setBusy(false)
      feedback.current?.focus()
    },
  })

  useLayoutEffect(() => {
    if (confirming) confirmationHeading.current?.focus()
    else if (restoreFocus.current) {
      const target = cancelButton.current?.disabled ? progressHeading.current : cancelButton.current
      target?.focus()
      restoreFocus.current = false
    }
  }, [confirming])

  function closeConfirmation() {
    if (submitting.current || !active.current || !isSessionCurrent()) return
    restoreFocus.current = true
    setConfirming(false)
  }

  const canCancel = Boolean(data?.available && data.queued > 0 && !status.isError)
  return (
    <section aria-labelledby={`${id}-heading`} className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <h2 ref={progressHeading} id={`${id}-heading`} tabIndex={-1} className="font-display text-lg font-semibold focus-visible:outline-2 focus-visible:outline-ring">Email progress</h2>
      <p className="text-sm text-muted-foreground">SMTP acceptance does not confirm delivery to an inbox. Cancellation only stops unclaimed queued email; claimed work, including expired claims, may still reach SMTP. In-app notifications are unchanged.</p>
      {receipt.emails_sent === 0 ? <p className="text-sm">Email progress is unavailable. No email targets were queued.</p> : (
        <>
          {data?.available ? (
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              {[
                ['Queued', data.queued], ['Claimed', data.claimed], ['SMTP accepted', data.smtp_accepted],
                ['Failed', data.failed], ['Cancelled', data.cancelled],
              ].map(([label, count]) => (
                <div key={label}><dt className="text-sm text-muted-foreground">{label}</dt><dd className="mt-1 text-2xl font-semibold tabular-nums">{count.toLocaleString()}</dd></div>
              ))}
            </dl>
          ) : data ? <p className="text-sm">Email progress is unavailable.</p> : status.isPending ? <p role="status" className="text-sm">Loading email progress...</p> : null}
          <div ref={feedback} tabIndex={-1} role={cancellation.isError || status.isError ? 'alert' : 'status'} className="flex flex-col gap-2 text-sm focus-visible:outline-2 focus-visible:outline-ring">
            {cancellation.isError ? <p>Could not confirm cancellation. Refreshed counts may show that some targets were cancelled. Review the current status before trying again.</p> : cancellation.data ? (
              <p>{cancellation.data.available ? `Cancelled ${cancellation.data.cancelled_now.toLocaleString()} queued email targets in this request. Any remaining queued targets can be reviewed again; claimed work cannot be stopped.` : 'Email progress is unavailable.'}</p>
            ) : null}
            {status.isError ? <p>Could not refresh email progress. Counts may be out of date. Refresh to try again.</p> : null}
            {busy ? <p>Cancelling unclaimed email and refreshing progress...</p> : null}
          </div>
          <div className="flex flex-wrap gap-3">
            <Button type="button" variant="outline" className={buttonMotion} disabled={busy} aria-disabled={busy || status.isFetching} onClick={() => { if (!busy && !status.isFetching) void status.refetch() }}>{status.isFetching && !busy ? 'Refreshing progress...' : 'Refresh email progress'}</Button>
            <Button ref={cancelButton} type="button" variant="outline" className={buttonMotion} disabled={busy || !canCancel || confirming} onClick={() => { cancellation.reset(); setConfirming(true) }}>Cancel queued email</Button>
          </div>
          {confirming ? (
            <section aria-labelledby={`${id}-confirm`} className="flex flex-col gap-3 rounded-xl border border-border bg-background p-4" onKeyDown={(event) => {
              if (event.key === 'Escape') { event.preventDefault(); closeConfirmation() }
            }}>
              <h3 ref={confirmationHeading} id={`${id}-confirm`} tabIndex={-1} className="font-medium focus-visible:outline-2 focus-visible:outline-ring">Cancel queued email?</h3>
              <p className="text-sm">Only targets still queued without a claim will be cancelled. Workers may claim more before this request completes, so fewer targets may be cancelled than currently shown. Claimed email and in-app notifications cannot be recalled.</p>
              {!canCancel ? <p className="text-sm">No cancellable targets are currently confirmed. Refresh progress before another attempt.</p> : null}
              <div className="flex flex-wrap gap-3">
                <Button type="button" variant="outline" className={buttonMotion} disabled={busy} onClick={closeConfirmation}>Keep queued email</Button>
                <Button type="button" variant="destructive" className={buttonMotion} disabled={busy || !canCancel} onClick={() => {
                  if (submitting.current || !active.current || !isSessionCurrent()) return
                  submitting.current = true
                  setBusy(true)
                  feedback.current?.focus()
                  cancellation.mutate(receipt.broadcast_id)
                }}>{busy ? 'Cancelling queued email...' : 'Confirm cancel queued email'}</Button>
              </div>
            </section>
          ) : null}
        </>
      )}
    </section>
  )
}
