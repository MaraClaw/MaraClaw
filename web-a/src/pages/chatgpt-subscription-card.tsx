import { skipToken, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import {
  ChatGPTSubscriptionError, getChatGPTSubscriptionStatus, startChatGPTSubscription,
  type ChatGPTSubscriptionStart,
} from '@/lib/chatgpt-subscription-api'

const pollIntervalMs = 2_000
// Stop shortly after the link's lifetime even when no answer has said it expired.
const expiryGraceMs = 10_000
// Consecutive failed checks before the admin is told; checking continues until expiry.
const failuresBeforeNotice = 3
const restartMessages = {
  expired: 'This ChatGPT sign-in has expired. Restart to get a new sign-in link.',
  denied: 'ChatGPT sign-in was denied. Restart when you are ready to try again.',
  error: 'ChatGPT sign-in could not be completed. Restart to try again.',
} as const

// Network failures and 5xx answers can pass, so they retry on the poll cadence instead of ending the attempt.
function isTransient(failure: Error) {
  return failure instanceof ChatGPTSubscriptionError && (failure.status === null || failure.status >= 500)
}

export function ChatGPTSubscriptionCard({ tenantId, platformAdmin }: {
  readonly tenantId: string
  readonly platformAdmin: boolean
}) {
  const { t, owned, number } = useTranslation()
  const id = useId()
  const queryClient = useQueryClient()
  const isCurrent = useSessionCurrent()
  const mounted = useRef(true)
  const content = useRef<HTMLDivElement>(null)
  const link = useRef<HTMLAnchorElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const [attempt, setAttempt] = useState<ChatGPTSubscriptionStart | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [statusMessage, setStatusMessage] = useState<string | null>(null)
  const [restart, setRestart] = useState(false)
  const [success, setSuccess] = useState(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const active = () => mounted.current && isCurrent()

  const start = useSessionMutation({
    gcTime: 0, retry: false,
    mutationFn: () => startChatGPTSubscription(platformAdmin ? tenantId : undefined),
    onSuccess: (started) => { if (active()) setAttempt(started) },
    onError: () => { if (active()) setError('Could not start ChatGPT sign-in. Check your connection and try again.') },
    onSettled: (): void => { start.reset() },
  })
  // Only a live attempt polls. Ending it (an outcome, local expiry or restart) changes the key, which
  // drops the old observer and aborts its request; unmount and session retirement do the same.
  const polling = attempt && !restart ? attempt : null
  const status = useQuery({
    queryKey: ['admin-chatgpt-subscription', tenantId, polling?.attempt_id ?? null],
    queryFn: polling ? ({ signal }) => getChatGPTSubscriptionStatus(polling.attempt_id, signal) : skipToken,
    gcTime: 0,
    retry: (_failures, failure) => isTransient(failure),
    retryDelay: pollIntervalMs,
    // Re-armed after every settled check, so checks never overlap. The admin signs in on another tab, so keep polling while this one is hidden.
    refetchIntervalInBackground: true,
    refetchInterval: (query) => query.state.status === 'success' && query.state.data?.status === 'pending' ? pollIntervalMs : false,
  })
  const outcome = polling === null ? null : status.isError ? 'unavailable' : status.data?.status ?? null

  useEffect(() => {
    if (outcome === null || outcome === 'pending' || !isCurrent()) return
    if (outcome === 'authorized') {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models', tenantId] })
      setAttempt(null)
      setSuccess(true)
      setError(null)
      setStatusMessage(null)
      return
    }
    setRestart(true)
    if (outcome === 'unavailable') setError('This sign-in attempt is no longer available. Restart to try again.')
    else setStatusMessage(restartMessages[outcome])
  }, [outcome, isCurrent, queryClient, tenantId])

  useEffect(() => {
    if (polling === null) return
    const timer = window.setTimeout(() => {
      if (!isCurrent()) return
      setRestart(true)
      setStatusMessage(restartMessages.expired)
    }, polling.expires_in * 1000 + expiryGraceMs)
    return () => window.clearTimeout(timer)
  }, [polling, isCurrent])

  const busy = start.isPending
  useEffect(() => {
    if (busy) return
    // Move focus only when it was lost or is already in this card; never take it from elsewhere.
    const focused = document.activeElement
    if (focused !== null && focused !== document.body && !content.current?.contains(focused)) return
    if (polling) link.current?.focus()
    else if (success || restart || error !== null) trigger.current?.focus()
  }, [busy, polling, restart, success, error])

  const begin = () => {
    if (busy) return
    setAttempt(null)
    setError(null)
    setStatusMessage(null)
    setRestart(false)
    setSuccess(false)
    start.mutate()
  }

  const statusText = success
    ? t('ChatGPT subscription connected. It is in the company pool.')
    : polling ? t('Waiting for sign-in…') : statusMessage ? owned(statusMessage) : ''

  return (
    <Card aria-labelledby={`${id}-title`}>
      <CardHeader>
        <CardTitle id={`${id}-title`}>{t('Connect ChatGPT subscription')}</CardTitle>
        <CardDescription>
          {t('Sign in with ChatGPT Plus, Pro, or Team in your browser. Access tokens stay on the server and are stored encrypted for this company.')}
        </CardDescription>
      </CardHeader>
      <CardContent ref={content} className="flex flex-col gap-3" aria-busy={busy}>
        {polling ? (
          <div className="grid gap-1.5 text-sm">
            <p>
              <a ref={link} className="font-medium text-primary underline underline-offset-4" href={polling.authorization_url}
                target="_blank" rel="noopener noreferrer" aria-describedby={`${id}-help`}>{t('Open ChatGPT sign-in (new tab)')}</a>
            </p>
            <p id={`${id}-help`} className="text-muted-foreground">
              {t('Use the new tab to finish signing in. This page updates by itself. The link expires in about {minutes} minutes.', { minutes: number(Math.ceil(polling.expires_in / 60)) })}
            </p>
          </div>
        ) : null}
        <p className="flex items-center gap-2 text-sm text-muted-foreground empty:sr-only" role="status">
          {polling ? <Loader2 className="size-4 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden /> : null}
          {statusText}
        </p>
        {polling && status.failureCount >= failuresBeforeNotice
          ? <ErrorToast message={t('Could not check ChatGPT sign-in. Check your connection. This page keeps trying until the link expires.')} />
          : null}
        {error ? <ErrorToast message={owned(error)} /> : null}
        <div>
          <Button ref={trigger} type="button" variant={attempt ? 'outline' : 'default'}
            disabled={busy || !tenantId} onClick={begin}>
            {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
            {busy ? t('Starting ChatGPT sign-in…') : attempt ? t('Restart ChatGPT sign-in') : t('Connect ChatGPT subscription')}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
