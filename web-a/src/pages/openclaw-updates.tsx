import { zodResolver } from '@hookform/resolvers/zod'
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { ZodError } from 'zod'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { ApiError } from '@/lib/http'
import { createOpenClawUpdateStorage } from '@/lib/openclaw-update-storage'
import { openClawUpdateDraftSchema, sendOpenClawUpdate } from '@/lib/openclaw-updates-api'
import type { OpenClawUpdateDraft, OpenClawUpdateRequest, OpenClawUpdateResult } from '@/lib/openclaw-updates-api'

const emptyDraft = { title: '', body: '', mandatory: false }
const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'
const audience = 'All active end users across every organization, including organization admins. Platform admins are excluded.'

export function OpenClawUpdatesPage() {
  const id = useId()
  const { t, owned, number, date } = useTranslation()
  const { user } = useAuth()
  const [storage] = useState(() => createOpenClawUpdateStorage(user?.id))
  const [restored] = useState<Readonly<{ request: OpenClawUpdateRequest | null; error: Error | null }>>(() => {
    try { return { request: storage.read(), error: null } }
    catch (error) {
      if (!(error instanceof Error)) throw error
      return { request: null, error }
    }
  })
  const [storageError, setStorageError] = useState(restored.error)
  const canDiscardSavedRequest = storageError instanceof SyntaxError || storageError instanceof ZodError
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const submitting = useRef(false)
  const request = useRef<OpenClawUpdateRequest | null>(restored.request)
  const focusRequested = useRef<'review' | 'draft' | 'feedback' | null>(null)
  const reviewHeading = useRef<HTMLHeadingElement>(null)
  const feedback = useRef<HTMLDivElement>(null)
  const [snapshot, setSnapshot] = useState<OpenClawUpdateDraft | null>(restored.request)
  const [result, setResult] = useState<OpenClawUpdateResult | null>(null)
  const [sendError, setSendError] = useState<Error | null>(restored.request ? new Error('Unconfirmed previous submission') : null)
  const form = useForm<OpenClawUpdateDraft>({ resolver: zodResolver(openClawUpdateDraftSchema), defaultValues: restored.request ?? emptyDraft })
  const { errors } = form.formState
  const title = form.watch('title')
  const body = form.watch('body')

  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const sending = useSessionMutation({
    mutationFn: sendOpenClawUpdate,
    retry: false,
    onSuccess: (data) => {
      if (!active.current || !isSessionCurrent()) return
      focusRequested.current = 'feedback'
      try { storage.clear(); setStorageError(null) }
      catch (error) {
        if (!(error instanceof Error)) throw error
        setStorageError(error)
      }
      request.current = null
      setSnapshot(null)
      setSendError(null)
      setResult(data)
      form.reset(emptyDraft)
    },
    onError: (error) => {
      if (!active.current || !isSessionCurrent()) return
      focusRequested.current = 'feedback'
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
      case 'feedback': feedback.current?.focus(); break
      case null: return
    }
    focusRequested.current = null
  })

  function editNewNotice() {
    if (submitting.current || !active.current || !isSessionCurrent()) return
    try { storage.clear(); setStorageError(null) }
    catch (error) {
      if (!(error instanceof Error)) throw error
      focusRequested.current = 'feedback'
      setStorageError(error)
      return
    }
    focusRequested.current = 'draft'
    request.current = null
    setSnapshot(null)
    setSendError(null)
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Updates')}</h1>
        <p className="text-muted-foreground">{t('Send a global upgrade notice. This is separate from an organization broadcast.')}</p>
      </header>
      <div ref={feedback} tabIndex={-1} role={sendError || storageError ? 'alert' : 'status'} className="flex flex-col gap-4 empty:hidden focus-visible:outline-2 focus-visible:outline-ring">
        {storageError ? <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4 text-sm">
          <h2 className="font-medium text-destructive">{t('Notice recovery unavailable')}</h2>
          <p>{result ? t('Delivery is confirmed, but the saved retry could not be cleared.') : t('Browser storage is unavailable or the saved request is invalid. Sending is blocked until this tab can safely save its retry request.')} {t('Enable browser storage and reload this page to recover the notice.')}{canDiscardSavedRequest ? ` ${t('You can intentionally compose a new notice instead. A separate send may duplicate an earlier delivery.')}` : null}</p>
          {canDiscardSavedRequest && !snapshot && !result ? <Button type="button" variant="outline" className={`self-start ${actionClass}`} onClick={editNewNotice}>{t('Compose a new notice')}</Button> : null}
        </div> : null}
        {sendError ? (
          <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4 text-sm">
            <h2 className="font-medium text-destructive">{t('Notice not confirmed')}</h2>
            <ErrorToast error={sendError} message={sendError instanceof ApiError ? sendError.message : t('The server response could not be confirmed. Check your connection.')} />
            <p>{t('Retry this notice after resolving the error. The same request ID and message will be reused, so a completed send is not duplicated.')}</p>
            <p>{t('The request is saved in this tab for your admin account, including across navigation and reloads. Keep this tab open until confirmed. Composing a new notice starts a separate send and may duplicate a notice already delivered.')}</p>
          </div>
        ) : result ? (
          <div className="flex min-w-0 flex-col gap-4 rounded-xl border border-border bg-card p-5">
            <h2 className="font-display text-lg font-semibold">{t('Notice sent')}</h2>
            <dl className="flex min-w-0 flex-col gap-4">
              <div><dt className="text-sm text-muted-foreground">{t('Recipients')}</dt><dd className="mt-1 text-2xl font-semibold tabular-nums">{number(result.recipients)}</dd></div>
              <div><dt className="text-sm text-muted-foreground">{t('Title')}</dt><dd className="mt-1 whitespace-pre-wrap font-medium">{result.notice.title}</dd></div>
              <div><dt className="text-sm text-muted-foreground">{t('Body')}</dt><dd className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{result.notice.body || t('No body')}</dd></div>
              <div><dt className="text-sm text-muted-foreground">{t('Upgrade requirement')}</dt><dd className="mt-1 text-sm">{result.notice.mandatory ? t('Mandatory') : t('Optional')}</dd></div>
              <div><dt className="text-sm text-muted-foreground">{t('Sent at')}</dt><dd className="mt-1 text-sm"><time dateTime={result.notice.created_at}>{date(result.notice.created_at)}</time></dd></div>
            </dl>
          </div>
        ) : null}
      </div>
      {snapshot ? (
        <Card role="region" aria-labelledby={`${id}-review`}>
          <CardHeader>
            <h2 ref={reviewHeading} id={`${id}-review`} tabIndex={-1} className="font-display text-lg font-semibold focus-visible:outline-2 focus-visible:outline-ring">{t('Review update notice')}</h2>
            <CardDescription>{t(audience)}</CardDescription>
          </CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-5">
            <dl className="flex min-w-0 flex-col gap-4">
              <div><dt className="text-sm text-muted-foreground">{t('Title')}</dt><dd className="mt-1 whitespace-pre-wrap font-medium">{snapshot.title}</dd></div>
              <div><dt className="text-sm text-muted-foreground">{t('Body')}</dt><dd className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{snapshot.body || t('No body')}</dd></div>
              <div><dt className="text-sm text-muted-foreground">{t('Upgrade requirement')}</dt><dd className="mt-1 font-medium">{snapshot.mandatory ? t('Mandatory') : t('Optional')}</dd></div>
            </dl>
            <p className="text-sm text-muted-foreground">{snapshot.mandatory ? t('Mandatory blocks agent work until the end user upgrades in the agent’s Settings and the upgrade completes.') : t('Optional lets end users continue agent work without upgrading.')} {t('Sending a notice does not automatically restart agents.')}</p>
            <div className="flex flex-wrap gap-3">
              <Button type="button" variant="outline" className={actionClass} disabled={sending.isPending} onClick={editNewNotice}>{sendError || storageError ? t('Compose a new notice') : t('Back to edit')}</Button>
              <Button type="button" className={`${actionClass} sm:ml-auto`} disabled={sending.isPending} onClick={() => {
                if (submitting.current || !active.current || !isSessionCurrent()) return
                request.current ??= Object.freeze({ ...snapshot, request_id: crypto.randomUUID() })
                try { storage.save(request.current); setStorageError(null) }
                catch (error) {
                  if (!(error instanceof Error)) throw error
                  focusRequested.current = 'feedback'
                  setStorageError(error)
                  return
                }
                submitting.current = true
                sending.mutate(request.current)
              }}>{sending.isPending ? t('Sending notice…') : request.current ? t('Retry this notice') : t('Send update notice')}</Button>
            </div>
            <p role="status" className="text-sm text-muted-foreground">{sending.isPending ? t('Sending the reviewed notice. Please wait; requests are not retried automatically.') : null}</p>
          </CardContent>
        </Card>
      ) : storageError && !result ? null : (
        <Card>
          <CardHeader>
            <h2 className="font-display text-lg font-semibold">{t('Compose update notice')}</h2>
            <CardDescription>{t(audience)}</CardDescription>
          </CardHeader>
          <CardContent>
            <form noValidate className="flex flex-col gap-5" onSubmit={form.handleSubmit((values) => {
              if (submitting.current || !active.current || !isSessionCurrent()) return
              focusRequested.current = 'review'
              setResult(null)
              setSnapshot(Object.freeze({ ...values }))
            })}>
              <div className="flex flex-col gap-2">
                <Label htmlFor={`${id}-title`}>{t('Title')}</Label>
                <Input id={`${id}-title`} autoComplete="off" aria-required="true" aria-invalid={Boolean(errors.title)} aria-describedby={`${id}-title-help ${errors.title ? `${id}-title-error` : ''}`} {...form.register('title')} />
          <p id={`${id}-title-help`} className="text-xs text-muted-foreground">{t('Required. {count}/200 characters.', { count: number(title.length) })}</p>
                {errors.title ? <p id={`${id}-title-error`} role="alert" className="text-sm text-destructive">{owned(errors.title.message)}</p> : null}
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor={`${id}-body`}>{t('Body (optional)')}</Label>
                <textarea id={`${id}-body`} rows={7} aria-invalid={Boolean(errors.body)} aria-describedby={`${id}-body-help ${errors.body ? `${id}-body-error` : ''}`} className="min-h-36 w-full min-w-0 resize-y rounded-xl border border-input bg-card px-3.5 py-2 text-base shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 aria-invalid:border-destructive sm:text-sm" {...form.register('body')} />
          <p id={`${id}-body-help`} className="text-xs text-muted-foreground">{t('{count}/1,000 characters.', { count: number(body.length) })}</p>
                {errors.body ? <p id={`${id}-body-error`} role="alert" className="text-sm text-destructive">{owned(errors.body.message)}</p> : null}
              </div>
              <fieldset className="flex min-w-0 flex-col gap-3">
                <legend className="mb-2 text-sm font-medium">{t('Upgrade requirement')}</legend>
                <Controller name="mandatory" control={form.control} render={({ field }) => (
                  <>
                    <label className="flex min-h-11 cursor-pointer items-start gap-3 text-sm" htmlFor={`${id}-optional`}>
                      <input ref={field.ref} id={`${id}-optional`} type="radio" name={field.name} checked={!field.value} onBlur={field.onBlur} onChange={() => field.onChange(false)} aria-describedby={`${id}-optional-help`} className="mt-0.5 size-5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" />
                      <span><span className="font-medium">{t('Optional')}</span><span id={`${id}-optional-help`} className="mt-1 block text-muted-foreground">{t('End users can continue agent work without upgrading.')}</span></span>
                    </label>
                    <label className="flex min-h-11 cursor-pointer items-start gap-3 text-sm" htmlFor={`${id}-mandatory`}>
                      <input id={`${id}-mandatory`} type="radio" name={field.name} checked={field.value} onBlur={field.onBlur} onChange={() => field.onChange(true)} aria-describedby={`${id}-mandatory-help`} className="mt-0.5 size-5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" />
                      <span><span className="font-medium">{t('Mandatory')}</span><span id={`${id}-mandatory-help`} className="mt-1 block text-muted-foreground">{t('Blocks agent work until the end user upgrades in the agent’s Settings and the upgrade completes.')}</span></span>
                    </label>
                  </>
                )} />
                <p className="text-sm text-muted-foreground">{t('Neither option automatically restarts agents. End users start the upgrade themselves in the agent’s Settings.')}</p>
              </fieldset>
              <Button type="submit" className={`self-start ${actionClass}`}>{t('Review update notice')}</Button>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
