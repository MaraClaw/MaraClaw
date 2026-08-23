import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2, Mail } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { memberAuthHighlights } from '@/lib/auth-highlights'
import { resendVerificationRequest, verifyEmailRequest } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const schema = z.object({
  token: z.string().trim().min(6, 'Enter the 6-digit code').max(512),
})

type FormValues = z.infer<typeof schema>

type VerifyState = { email?: string }

export function VerifyEmailPage() {
  const formId = useId()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const { applySession } = useAuth()
  const emailFromState = (location.state as VerifyState | null)?.email
  const codeFromQuery = useMemo(() => searchParams.get('code')?.trim() ?? '', [searchParams])
  const [email, setEmail] = useState(emailFromState ?? '')
  const [formError, setFormError] = useState<string | null>(null)
  const [resending, setResending] = useState(false)

  const {
    register,
    handleSubmit,
    setValue,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { token: codeFromQuery },
  })

  useEffect(() => {
    if (codeFromQuery) setValue('token', codeFromQuery)
  }, [codeFromQuery, setValue])

  async function consume(token: string) {
    const session = await verifyEmailRequest(token)
    applySession(session)
    toast.success('Email verified')
    navigate(session.needs_org_confirm ? '/join' : '/app', { replace: true })
  }

  useEffect(() => {
    if (!codeFromQuery) return
    let cancelled = false
    void verifyEmailRequest(codeFromQuery)
      .then((session) => {
        if (cancelled) return
        applySession(session)
        toast.success('Email verified')
        navigate(session.needs_org_confirm ? '/join' : '/app', { replace: true })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setFormError(
          error instanceof ApiError
            ? (formatApiDetail(error.detail) ?? 'This verification code is invalid or expired.')
            : 'Unable to verify. Try again.',
        )
      })
    return () => {
      cancelled = true
    }
  }, [applySession, codeFromQuery, navigate])

  async function onSubmit(values: FormValues) {
    setFormError(null)
    try {
      await consume(values.token)
    } catch (error) {
      setFormError(
        error instanceof ApiError
          ? (formatApiDetail(error.detail) ?? 'This verification code is invalid or expired.')
          : 'Unable to verify. Try again.',
      )
    }
  }

  async function resend() {
    if (!email.trim()) {
      setFormError('Enter the email you registered with, then resend.')
      return
    }
    setResending(true)
    setFormError(null)
    try {
      await resendVerificationRequest(email.trim())
      toast.success('If that account exists, a new code is on the way.')
    } catch (error) {
      setFormError(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Unable to resend.')
    } finally {
      setResending(false)
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title="Verify your email"
      description="Enter the 6-digit code we sent, or open the link from the message."
      brandTitle="Confirm it is you"
      brandBody="Unverified accounts cannot start a workspace session."
      highlights={memberAuthHighlights}
    >
      {formError ? (
        <div role="alert" className="mb-4 rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive">
          {formError}
        </div>
      ) : null}

      <form className="space-y-4" onSubmit={handleSubmit(onSubmit)} noValidate>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-code`}>Verification code</Label>
          <Input
            id={`${formId}-code`}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="123456"
            disabled={isSubmitting}
            {...register('token')}
          />
          {errors.token ? <p className="text-xs text-destructive">{errors.token.message}</p> : null}
        </div>
        <Button type="submit" className="h-11 w-full" disabled={isSubmitting}>
          {isSubmitting ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Verifying…
            </>
          ) : (
            'Verify email'
          )}
        </Button>
      </form>

      <div className="mt-6 space-y-3 border-t border-border pt-6">
        <Label htmlFor={`${formId}-email`}>Email for a new code</Label>
        <div className="relative">
          <Mail className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input
            id={`${formId}-email`}
            type="email"
            className="pl-10"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <Button type="button" variant="outline" className="h-11 w-full" disabled={resending} onClick={() => void resend()}>
          {resending ? 'Sending…' : 'Resend code'}
        </Button>
      </div>

      <p className="mt-6 text-center text-sm text-muted-foreground">
        <Link to="/login" className="font-medium text-primary underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </p>
    </AuthShell>
  )
}
