import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeftRight, Building2, Loader2, Lock } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { lookupOrgByEmail, transferOrg } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'
import type { SuggestedOrg } from '@/lib/types/auth'

const transferSchema = z.object({
  invitation_code: z.string().trim().max(128).optional(),
  password: z.string().min(1, 'Confirm your current password'),
})

type TransferFormValues = z.infer<typeof transferSchema>

const highlights = [
  {
    icon: ArrowLeftRight,
    title: 'Move with an invite',
    body: 'Paste a company invitation code and confirm your password.',
  },
  {
    icon: Building2,
    title: 'Or match your email',
    body: 'Transfer to the company that claimed your domain, or to OpenClaw.',
  },
  {
    icon: Lock,
    title: 'Password required',
    body: 'Every transfer asks for your current password.',
  },
]

export function TransferPage() {
  const { status, user, applySession } = useAuth()
  const navigate = useNavigate()
  const formId = useId()
  const [match, setMatch] = useState<SuggestedOrg | null>(null)
  const [fallback, setFallback] = useState<SuggestedOrg | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [pendingTarget, setPendingTarget] = useState<string | null>(null)

  const {
    register,
    handleSubmit,
    getValues,
    formState: { errors, isSubmitting },
  } = useForm<TransferFormValues>({
    resolver: zodResolver(transferSchema),
    defaultValues: { invitation_code: '', password: '' },
  })

  useEffect(() => {
    if (status === 'anonymous') {
      navigate('/login', { replace: true })
      return
    }
    if (status !== 'authenticated' || !user?.email) return
    let cancelled = false
    void lookupOrgByEmail(user.email)
      .then((lookup) => {
        if (cancelled) return
        setMatch(lookup.match)
        setFallback(lookup.fallback)
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 401) navigate('/login', { replace: true })
      })
    return () => {
      cancelled = true
    }
  }, [navigate, status, user])

  async function runTransfer(body: { password: string; invitation_code?: string; tenant_id?: string }) {
    setFormError(null)
    const result = await transferOrg(body)
    applySession(result)
    toast.success('Organization updated')
    navigate('/app', { replace: true })
  }

  async function onSubmit(values: TransferFormValues) {
    const code = values.invitation_code?.trim()
    if (!code) {
      setFormError('Enter an invitation code, or choose a destination below.')
      return
    }
    setPendingTarget('invite')
    try {
      await runTransfer({ password: values.password, invitation_code: code })
    } catch (err) {
      setFormError(
        err instanceof ApiError ? (formatApiDetail(err.detail) ?? err.message) : 'Transfer failed',
      )
    } finally {
      setPendingTarget(null)
    }
  }

  async function transferTo(org: SuggestedOrg) {
    const password = getValues('password')
    if (!password) {
      setFormError('Enter your current password first.')
      return
    }
    setPendingTarget(org.id)
    try {
      await runTransfer({ password, tenant_id: org.id })
    } catch (err) {
      setFormError(
        err instanceof ApiError ? (formatApiDetail(err.detail) ?? err.message) : 'Transfer failed',
      )
    } finally {
      setPendingTarget(null)
    }
  }

  const busy = isSubmitting || pendingTarget !== null

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title="Transfer organization"
      description="Move with an invitation code, or to your email-domain company / OpenClaw."
      brandTitle="Change where you work"
      brandBody="Keep the same MaraClaw account and switch companies when you have an invite or a matching domain."
      highlights={highlights}
    >
      {formError ? (
        <div
          role="alert"
          className="mb-4 rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive"
        >
          {formError}
        </div>
      ) : null}

      <form className="space-y-4" onSubmit={handleSubmit(onSubmit)}>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-invite`}>Invitation code</Label>
          <Input id={`${formId}-invite`} placeholder="Optional" {...register('invitation_code')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-password`}>Current password</Label>
          <Input id={`${formId}-password`} type="password" autoComplete="current-password" {...register('password')} />
          {errors.password ? <p className="text-xs text-destructive">{errors.password.message}</p> : null}
        </div>
        <Button type="submit" className="h-11 w-full" disabled={busy}>
          {pendingTarget === 'invite' ? <Loader2 className="size-4 animate-spin" /> : null}
          Transfer with invite
        </Button>
      </form>

      <div className="mt-4 flex flex-col gap-2">
        {match ? (
          <Button variant="outline" disabled={busy} onClick={() => void transferTo(match)}>
            {pendingTarget === match.id ? <Loader2 className="size-4 animate-spin" /> : null}
            Transfer to {match.name}
          </Button>
        ) : null}
        {fallback ? (
          <Button variant="outline" disabled={busy} onClick={() => void transferTo(fallback)}>
            {pendingTarget === fallback.id ? <Loader2 className="size-4 animate-spin" /> : null}
            Transfer to {fallback.name}
          </Button>
        ) : null}
      </div>

      <p className="mt-6 text-center text-sm">
        <Link to="/app" className="font-medium text-primary underline-offset-4 hover:underline">
          Back to workspace
        </Link>
      </p>
    </AuthShell>
  )
}
