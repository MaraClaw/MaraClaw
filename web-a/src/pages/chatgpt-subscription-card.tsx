import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import {
  ChatGPTSubscriptionError, completeChatGPTSubscription, getChatGPTSubscriptionStatus,
  startChatGPTSubscription, type ChatGPTSubscriptionComplete, type ChatGPTSubscriptionStart,
} from '@/lib/chatgpt-subscription-api'
import { StaleSessionError } from '@/lib/session-policy'

const schema = z.object({
  callback_url: z.string().trim().pipe(z.url({
    protocol: /^https?$/u, error: 'Paste the full callback address from your browser address bar.',
  })),
})

export function ChatGPTSubscriptionCard({ tenantId, platformAdmin }: {
  readonly tenantId: string
  readonly platformAdmin: boolean
}) {
  const id = useId()
  const queryClient = useQueryClient()
  const isCurrent = useSessionCurrent()
  const mounted = useRef(true)
  const submitted = useRef<ChatGPTSubscriptionComplete | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const [attempt, setAttempt] = useState<ChatGPTSubscriptionStart | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [restart, setRestart] = useState(false)
  const [success, setSuccess] = useState(false)
  const { register, handleSubmit, reset, setFocus, formState: { errors } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { callback_url: '' },
  })
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; submitted.current = null }
  }, [])
  const active = () => mounted.current && isCurrent()

  const start = useSessionMutation({
    gcTime: 0, retry: false,
    mutationFn: () => startChatGPTSubscription(platformAdmin ? tenantId : undefined),
    onSuccess: (started) => { if (active()) setAttempt(started) },
    onError: () => { if (active()) setError('Could not start ChatGPT sign-in. Check your connection and try again.') },
    onSettled: (): void => { start.reset() },
  })
  const complete = useSessionMutation({
    gcTime: 0, retry: false,
    // Void mutation variables: the callback never enters React Query's cache.
    mutationFn: async () => {
      const input = submitted.current
      submitted.current = null
      if (!input || !active()) throw new StaleSessionError()
      try {
        return (await completeChatGPTSubscription(input)).status
      } catch (failure) {
        if (!active()) throw new StaleSessionError()
        if (!(failure instanceof ChatGPTSubscriptionError)) throw failure
        if (failure.status !== null && failure.status < 500) throw failure
        // A response may have been lost after the server saved the model.
        // Read once before offering another submission; never poll or replay it.
        return (await getChatGPTSubscriptionStatus(input.attempt_id)).status
      }
    },
    onSuccess: (status) => {
      if (!active()) return
      switch (status) {
        case 'authorized':
          void queryClient.invalidateQueries({ queryKey: ['admin-llm-models', tenantId] })
          setAttempt(null)
          setSuccess(true)
          setError(null)
          break
        case 'pending':
          setError('Sign-in is still pending. Finish signing in, then paste the full callback address again, or restart.')
          break
        case 'expired':
          setRestart(true)
          setError('This ChatGPT sign-in has expired. Restart to get a new sign-in link.')
          break
        case 'denied':
          setRestart(true)
          setError('ChatGPT sign-in was denied. Restart when you are ready to try again.')
          break
        case 'error':
          setRestart(true)
          setError('ChatGPT sign-in could not be completed. Restart to try again.')
          break
        default: throw new Error(status satisfies never)
      }
    },
    onError: (failure) => {
      if (!active()) return
      if (failure instanceof ChatGPTSubscriptionError && failure.status === 400) {
        setError('The callback address was not accepted. Copy the full address for this sign-in and paste it again.')
      } else if (failure instanceof ChatGPTSubscriptionError && failure.status !== null && failure.status < 500) {
        setRestart(true)
        setError('This sign-in attempt is no longer available. Restart to try again.')
      } else {
        setError('Could not confirm sign-in after checking its status. Paste the address again to retry, or restart.')
      }
    },
    onSettled: (): void => { submitted.current = null; complete.reset() },
  })
  const busy = start.isPending || complete.isPending
  useEffect(() => {
    if (busy) return
    if (attempt && !restart) setFocus('callback_url')
    else if (success || restart) trigger.current?.focus()
  }, [attempt, busy, restart, success, setFocus])

  const begin = () => {
    if (busy) return
    reset()
    submitted.current = null
    setAttempt(null)
    setError(null)
    setRestart(false)
    setSuccess(false)
    start.mutate()
  }

  return (
    <Card aria-labelledby={`${id}-title`}>
      <CardHeader>
        <CardTitle id={`${id}-title`}>Connect ChatGPT subscription</CardTitle>
        <CardDescription>
          Sign in with ChatGPT Plus, Pro, or Team in your browser. Access tokens stay on the server
          and are stored encrypted for this company.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {success ? <p className="text-sm text-muted-foreground" role="status">ChatGPT subscription connected. It is in the company pool.</p> : null}
        {attempt && !restart ? (
          <form noValidate autoComplete="off" className="grid gap-3" aria-busy={complete.isPending}
            onSubmit={handleSubmit((values) => {
              if (busy || !active()) return
              submitted.current = { attempt_id: attempt.attempt_id, callback_url: values.callback_url }
              reset()
              setError(null)
              complete.mutate()
            })}>
            <p className="text-sm">
              <a className="font-medium text-primary underline underline-offset-4" href={attempt.authorization_url}
                target="_blank" rel="noopener noreferrer">Open ChatGPT sign-in (new tab)</a>
            </p>
            <p id={`${id}-help`} className="text-sm text-muted-foreground">
              After sign-in, the expected localhost page may fail to load. That is normal: copy the FULL
              address from that tab’s address bar and paste it below. No CLI or local listener is needed.
              The link expires in about {Math.ceil(attempt.expires_in / 60)} minutes.
            </p>
            <div className="grid gap-1.5">
              <Label htmlFor={`${id}-callback`}>Full callback URL</Label>
              <Input id={`${id}-callback`} type="text" autoComplete="off" spellCheck={false}
                disabled={complete.isPending} aria-invalid={Boolean(errors.callback_url)}
                aria-describedby={`${id}-help${errors.callback_url ? ` ${id}-invalid` : ''}${error ? ` ${id}-error` : ''}`}
                {...register('callback_url')} />
              {errors.callback_url ? <p id={`${id}-invalid`} className="text-sm text-destructive" role="alert">{errors.callback_url.message}</p> : null}
            </div>
            <div><Button type="submit" disabled={busy}>
              {complete.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              {complete.isPending ? 'Confirming ChatGPT sign-in…' : 'Complete ChatGPT sign-in'}
            </Button></div>
          </form>
        ) : null}
        {error ? <p id={`${id}-error`} className="text-sm text-destructive" role="alert">{error}</p> : null}
        <div>
          <Button ref={trigger} type="button" variant={attempt ? 'outline' : 'default'}
            disabled={busy || !tenantId} onClick={begin}>
            {start.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
            {start.isPending ? 'Starting ChatGPT sign-in…' : attempt ? 'Restart ChatGPT sign-in' : 'Connect ChatGPT subscription'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
