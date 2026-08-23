import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeft, Loader2, Mail } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { memberAuthHighlights } from '@/lib/auth-highlights'
import { forgotPasswordRequest } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const schema = z.object({
  email: z.string().trim().min(1, 'Enter your email').email('Enter a valid email').max(254),
})

type FormValues = z.infer<typeof schema>

export function ForgotPasswordPage() {
  const formId = useId()
  const [formError, setFormError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const {
    register,
    handleSubmit,
    getValues,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '' },
  })

  async function onSubmit(values: FormValues) {
    setFormError(null)
    try {
      await forgotPasswordRequest(values.email.trim().toLowerCase())
      setSent(true)
    } catch (error) {
      setFormError(
        error instanceof ApiError
          ? (formatApiDetail(error.detail) ??
            (error.status === 400
              ? 'Password reset is currently unavailable. Contact your operator.'
              : 'Unable to send reset email. Try again.'))
          : 'Unable to send reset email. Try again.',
      )
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title={sent ? 'Check your email' : 'Reset password'}
      description={
        sent
          ? 'If an account with that email exists, a reset link has been sent.'
          : 'Enter the email for your member account. We will send a reset link if it matches.'
      }
      brandTitle="Get back into your workspace"
      brandBody="Reset links from this site stay on MaraClaw member sign-in, not the admin console."
      highlights={memberAuthHighlights}
    >
      {formError ? (
        <div role="alert" className="mb-4 rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive">
          {formError}
        </div>
      ) : null}

      {sent ? (
        <div className="space-y-4">
          <p className="rounded-xl border border-border/80 bg-muted/40 px-3.5 py-3 text-sm text-muted-foreground">
            Sent to <span className="font-medium text-foreground">{getValues('email')}</span>. Check spam if
            you do not see it.
          </p>
          <Button asChild variant="outline" className="h-11 w-full">
            <Link to="/login">
              <ArrowLeft className="size-4" aria-hidden />
              Return to sign in
            </Link>
          </Button>
        </div>
      ) : (
        <form className="space-y-4" onSubmit={handleSubmit(onSubmit)} noValidate>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-email`}>Email</Label>
            <div className="relative">
              <Mail className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
              <Input
                id={`${formId}-email`}
                type="email"
                autoComplete="email"
                className="pl-10"
                disabled={isSubmitting}
                {...register('email')}
              />
            </div>
            {errors.email ? <p className="text-xs text-destructive">{errors.email.message}</p> : null}
          </div>
          <Button type="submit" className="h-11 w-full" disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                Sending link…
              </>
            ) : (
              'Send reset link'
            )}
          </Button>
        </form>
      )}

      <p className="mt-6 text-center text-sm text-muted-foreground">
        <Link to="/login" className="font-medium text-primary underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </p>
    </AuthShell>
  )
}
