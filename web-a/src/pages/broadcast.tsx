import { zodResolver } from '@hookform/resolvers/zod'
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { broadcastDraftSchema, sendBroadcast, type BroadcastDraft, type BroadcastResult } from '@/lib/broadcast-api'
import { ApiError } from '@/lib/http'

const emptyDraft = { title: '', body: '', send_email: false }
const buttonMotion = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'
const audience = 'Active users in your current organization, except you, plus all organization agents.'

export function BroadcastPage() {
  const id = useId()
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const submitting = useRef(false)
  const focusRequested = useRef<'review' | 'draft' | 'error' | 'success' | null>(null)
  const reviewHeading = useRef<HTMLHeadingElement>(null)
  const feedback = useRef<HTMLDivElement>(null)
  const [snapshot, setSnapshot] = useState<BroadcastDraft | null>(null)
  const [result, setResult] = useState<BroadcastResult | null>(null)
  const [sendError, setSendError] = useState<Error | null>(null)
  const form = useForm<BroadcastDraft>({ resolver: zodResolver(broadcastDraftSchema), defaultValues: emptyDraft })
  const { errors } = form.formState
  const title = form.watch('title')
  const body = form.watch('body')

  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const sending = useSessionMutation({
    mutationFn: sendBroadcast,
    retry: false,
    onSuccess: (data) => {
      if (!active.current || !isSessionCurrent()) return
      focusRequested.current = 'success'
      setSnapshot(null)
      setSendError(null)
      setResult(data)
      form.reset(emptyDraft)
      toast.success('Broadcast sent')
    },
    onError: (error) => {
      if (!active.current || !isSessionCurrent()) return
      focusRequested.current = 'error'
      setSnapshot(null)
      setSendError(error)
    },
    onSettled: () => {
      if (!active.current || !isSessionCurrent()) return
      submitting.current = false
    },
  })

  useLayoutEffect(() => {
    if (!active.current || !isSessionCurrent()) return
    switch (focusRequested.current) {
      case 'review': reviewHeading.current?.focus(); break
      case 'draft': form.setFocus('title'); break
      case 'error':
      case 'success': feedback.current?.focus(); break
      case null: return
    }
    focusRequested.current = null
  })

  function closeReview() {
    if (submitting.current || !active.current || !isSessionCurrent()) return
    focusRequested.current = 'draft'
    setSnapshot(null)
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Organization broadcast</h1>
        <p className="text-muted-foreground">Send an announcement to your current organization. Review the exact message before sending.</p>
      </header>
      <div ref={feedback} tabIndex={-1} role={sendError ? 'alert' : 'status'} className="empty:hidden focus-visible:outline-2 focus-visible:outline-ring">
        {sendError ? (
          <div className="flex flex-col gap-2 rounded-xl border border-destructive/30 bg-card p-4 text-sm">
            <p className="font-medium text-destructive">Broadcast failed: {sendError instanceof ApiError ? sendError.message : 'Could not confirm the broadcast result.'}</p>
            <p>Your draft is kept below. Review it again before another attempt. A failed request does not guarantee that nothing was sent.</p>
          </div>
        ) : result ? (
          <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
            <h2 className="font-display text-lg font-semibold">Broadcast sent</h2>
            <dl className="grid gap-4 sm:grid-cols-3">
              {[
                ['Users notified', result.users_notified],
                ['Agents notified', result.agents_notified],
                ['Queued email targets', result.emails_sent],
              ].map(([label, count]) => (
                <div key={label}><dt className="text-sm text-muted-foreground">{label}</dt><dd className="mt-1 text-2xl font-semibold tabular-nums">{count.toLocaleString()}</dd></div>
              ))}
            </dl>
            <p className="text-sm text-muted-foreground">Email targets are queued for processing, not confirmed deliveries.</p>
          </div>
        ) : null}
      </div>
      {snapshot ? (
        <Card role="region" aria-labelledby={`${id}-review`} onKeyDown={(event) => {
          if (event.key === 'Escape') { event.preventDefault(); closeReview() }
        }}>
          <CardHeader>
            <h2 ref={reviewHeading} id={`${id}-review`} tabIndex={-1} className="font-display text-lg font-semibold focus-visible:outline-2 focus-visible:outline-ring">Review broadcast</h2>
            <CardDescription>This message cannot be recalled after sending.</CardDescription>
          </CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-5">
            <div className="rounded-xl border border-border bg-background p-4">
              <h3 className="text-sm font-medium">Audience</h3>
              <p className="mt-1 text-sm text-muted-foreground">{audience}</p>
            </div>
            <dl className="flex min-w-0 flex-col gap-4">
              <div><dt className="text-sm text-muted-foreground">Title</dt><dd className="mt-1 whitespace-pre-wrap font-medium">{snapshot.title}</dd></div>
              <div><dt className="text-sm text-muted-foreground">Body</dt><dd className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{snapshot.body || 'No body'}</dd></div>
              <div><dt className="text-sm text-muted-foreground">Email</dt><dd className="mt-1 text-sm">{snapshot.send_email ? 'Also queue email for eligible user addresses' : 'In-app notifications only; email is off'}</dd></div>
            </dl>
            <p className="text-sm text-muted-foreground">Sending is not retried automatically. Email queue counts do not confirm delivery.</p>
            <div className="flex flex-wrap gap-3">
              <Button type="button" variant="outline" className={buttonMotion} disabled={sending.isPending} onClick={closeReview}>Back to edit</Button>
              <Button type="button" variant="ghost" className={buttonMotion} disabled={sending.isPending} onClick={closeReview}>Cancel</Button>
              <Button type="button" className={`${buttonMotion} sm:ml-auto`} disabled={sending.isPending} onClick={() => {
                if (submitting.current || !active.current || !isSessionCurrent()) return
                submitting.current = true
                sending.mutate(snapshot)
              }}>{sending.isPending ? 'Sending broadcast...' : 'Send broadcast'}</Button>
            </div>
            <p role="status" className="text-sm text-muted-foreground">{sending.isPending ? 'Sending your reviewed message. Please wait.' : null}</p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <h2 className="font-display text-lg font-semibold">Compose message</h2>
            <CardDescription>{audience}</CardDescription>
          </CardHeader>
          <CardContent>
            <form noValidate className="flex flex-col gap-5" onSubmit={form.handleSubmit((values) => {
              if (submitting.current || !active.current || !isSessionCurrent()) return
              focusRequested.current = 'review'
              setSendError(null)
              setResult(null)
              setSnapshot(Object.freeze({ ...values }))
            })}>
              <div className="flex flex-col gap-2">
                <Label htmlFor={`${id}-title`}>Title</Label>
                <Input id={`${id}-title`} autoComplete="off" aria-required="true" aria-invalid={Boolean(errors.title)} aria-describedby={`${id}-title-help ${errors.title ? `${id}-title-error` : ''}`} {...form.register('title')} />
                <p id={`${id}-title-help`} className="text-xs text-muted-foreground">Required. {title.length}/200 characters.</p>
                {errors.title ? <p id={`${id}-title-error`} role="alert" className="text-sm text-destructive">{errors.title.message}</p> : null}
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor={`${id}-body`}>Body (optional)</Label>
                <textarea id={`${id}-body`} rows={7} aria-invalid={Boolean(errors.body)} aria-describedby={`${id}-body-help ${errors.body ? `${id}-body-error` : ''}`} className="min-h-36 w-full min-w-0 resize-y rounded-xl border border-input bg-card px-3.5 py-2 text-base shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 aria-invalid:border-destructive sm:text-sm" {...form.register('body')} />
                <p id={`${id}-body-help`} className="text-xs text-muted-foreground">{body.length}/1,000 characters.</p>
                {errors.body ? <p id={`${id}-body-error`} role="alert" className="text-sm text-destructive">{errors.body.message}</p> : null}
              </div>
              <label htmlFor={`${id}-email`} className="flex min-h-11 cursor-pointer items-center gap-3 text-sm font-medium">
                <input id={`${id}-email`} type="checkbox" className="size-5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" aria-describedby={`${id}-email-help`} {...form.register('send_email')} />
                Also send email
              </label>
              <p id={`${id}-email-help`} className="-mt-3 text-sm text-muted-foreground">Optional. Queue email for eligible user addresses as well as in-app notifications.</p>
              <Button type="submit" className={`self-start ${buttonMotion}`}>Review broadcast</Button>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
