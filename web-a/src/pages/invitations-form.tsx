import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { sendInvitationOptions } from '@/hooks/invitation-options'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { INVITATION_TTL_DAYS, invitationDraftSchema, invitationRequest, type InvitationDraft, type InvitationDraftInput } from '@/lib/invitation-form-schema'
import { invitationSendFailure } from '@/lib/invitations-api'

export function InvitationsForm({ tenant }: { readonly tenant: string | null }) {
  const id = useId()
  const { user } = useAuth()
  const current = useSessionCurrent()
  const mounted = useRef(false)
  const submitting = useRef(false)
  const feedback = useRef<HTMLDivElement>(null)
  const focusFeedback = useRef(false)
  const [sent, setSent] = useState<number | null>(null)
  const [failure, setFailure] = useState<ReturnType<typeof invitationSendFailure> | null>(null)
  const form = useForm<InvitationDraftInput, undefined, InvitationDraft>({
    resolver: zodResolver(invitationDraftSchema), defaultValues: { emails: '', ttl_days: INVITATION_TTL_DAYS },
  })
  const { errors } = form.formState
  const options = sendInvitationOptions(useQueryClient(), tenant)
  const sending = useSessionMutation({
    ...options,
    onSuccess: (result) => {
      options.onSuccess()
      if (!mounted.current || !current()) return
      form.resetField('emails')
      setSent(result.invited)
      focusFeedback.current = true
    },
    onError: (error) => {
      if (!mounted.current || !current()) return
      setFailure(invitationSendFailure(error, user?.is_genesis === true))
      focusFeedback.current = true
    },
    onSettled: () => { if (mounted.current && current()) submitting.current = false },
  })
  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useLayoutEffect(() => {
    if (focusFeedback.current && current()) { feedback.current?.focus(); focusFeedback.current = false }
  })

  return (
    <Card>
      <CardHeader><h2 className="font-display text-lg font-semibold">Send invitations</h2></CardHeader>
      <CardContent>
        <form noValidate className="flex flex-col gap-5" onSubmit={form.handleSubmit((draft) => {
          if (submitting.current || !current()) return
          submitting.current = true
          setSent(null)
          setFailure(null)
          sending.mutate(invitationRequest(draft))
        })}>
          <div className="grid min-w-0 gap-5 sm:grid-cols-[minmax(0,1fr)_minmax(0,16rem)]">
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-emails`}>Recipient emails</Label>
              <Input id={`${id}-emails`} type="text" inputMode="email" autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="alice@example.com; bob@example.com" required disabled={sending.isPending} aria-invalid={Boolean(errors.emails)} aria-describedby={`${id}-emails-help ${errors.emails ? `${id}-emails-error` : ''}`} {...form.register('emails')} />
              <p id={`${id}-emails-help`} className="text-sm text-muted-foreground">Enter one email or separate emails with semicolons (;), with no empty entries. Duplicates are sent once. Each invitation is single use by its recipient only.</p>
              {errors.emails ? <p id={`${id}-emails-error`} role="alert" className="text-sm text-destructive">{errors.emails.message}</p> : null}
            </div>
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-ttl`}>Valid for (days)</Label>
              <Input id={`${id}-ttl`} type="number" inputMode="numeric" min={1} max={INVITATION_TTL_DAYS} step={1} required disabled={sending.isPending} aria-invalid={Boolean(errors.ttl_days)} aria-describedby={`${id}-ttl-help ${errors.ttl_days ? `${id}-ttl-error` : ''}`} {...form.register('ttl_days', { valueAsNumber: true })} />
              <p id={`${id}-ttl-help`} className="text-sm text-muted-foreground">1–14 whole days. Default and maximum: 14 days.</p>
              {errors.ttl_days ? <p id={`${id}-ttl-error`} role="alert" className="text-sm text-destructive">{errors.ttl_days.message}</p> : null}
            </div>
          </div>
          <Button type="submit" disabled={sending.isPending} className="min-h-11 self-start motion-reduce:transition-none">{sending.isPending ? 'Sending invitations…' : 'Send invitation emails'}</Button>
          <div ref={feedback} tabIndex={-1} role={failure ? 'alert' : 'status'} className="text-sm empty:hidden focus-visible:outline-2 focus-visible:outline-ring">
            {failure ? <><p className="text-destructive">{failure.message}</p>{failure.settings ? <Link to="/settings" className="mt-2 inline-flex min-h-11 items-center underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring">Open Settings</Link> : null}</> : sent !== null ? sent > 0 ? `${sent} ${sent === 1 ? 'invitation' : 'invitations'} created. Email delivery queued.` : 'No invitations were created. Check history before trying again.' : sending.isPending ? 'Sending. Please wait.' : null}
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
