import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeft, Loader2, Mail } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import { AuthShell } from '@/components/layout/auth-shell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { forgotPasswordRequest } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const schema = z.object({
  email: z
    .string()
    .trim()
    .min(1, 'Enter your email address')
    .email('Enter a valid email address')
    .max(254, 'Email is too long'),
})

type FormValues = z.infer<typeof schema>

export function ForgotPasswordPage() {
  const formId = useId()
  const emailId = `${formId}-email`
  const emailErrorId = `${formId}-email-error`
  const formErrorId = `${formId}-form-error`
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
      if (error instanceof ApiError) {
        setFormError(
          formatApiDetail(error.detail) ??
            (error.status === 400
              ? 'Password reset is currently unavailable. Contact your platform operator.'
              : 'Unable to send reset email. Try again.'),
        )
        return
      }
      setFormError('Unable to send reset email. Check your connection and try again.')
    }
  }

  if (sent) {
    const email = getValues('email')
    return (
      <AuthShell
        title="Check your email"
        description="If an account with that email exists, a password reset link has been sent."
        footer={
          <p className="text-center text-sm text-muted-foreground">
            <Link to="/login" className="font-medium text-primary underline-offset-4 hover:underline">
              Back to sign in
            </Link>
          </p>
        }
      >
        <div
          className="rounded-xl border border-border/80 bg-muted/40 px-3.5 py-3 text-sm text-muted-foreground"
          role="status"
        >
          Sent to <span className="font-medium text-foreground">{email}</span>. The link expires after a
          short time. Check spam if you do not see it.
        </div>
        <Button asChild variant="outline" className="mt-4 h-11 w-full">
          <Link to="/login">
            <ArrowLeft className="size-4" aria-hidden />
            Return to sign in
          </Link>
        </Button>
      </AuthShell>
    )
  }

  return (
    <AuthShell
      title="Reset password"
      description="Enter the email for your admin account. We will send a reset link if it matches an active identity."
      footer={
        <p className="text-center text-sm text-muted-foreground">
          <Link
            to="/login"
            className="inline-flex items-center gap-1.5 font-medium text-primary underline-offset-4 hover:underline"
          >
            <ArrowLeft className="size-3.5" aria-hidden />
            Back to sign in
          </Link>
        </p>
      }
    >
      {formError ? (
        <div
          id={formErrorId}
          role="alert"
          className="mb-4 rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive"
        >
          {formError}
        </div>
      ) : null}

      <form
        className="space-y-4"
        onSubmit={handleSubmit(onSubmit)}
        noValidate
        aria-describedby={formError ? formErrorId : undefined}
      >
        <div className="space-y-2">
          <Label htmlFor={emailId}>Email</Label>
          <div className="relative">
            <Mail
              className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              id={emailId}
              type="email"
              autoComplete="email"
              inputMode="email"
              placeholder="name@company.com"
              className="pl-10"
              aria-invalid={errors.email ? true : undefined}
              aria-describedby={errors.email ? emailErrorId : undefined}
              disabled={isSubmitting}
              {...register('email')}
            />
          </div>
          {errors.email ? (
            <p id={emailErrorId} className="text-xs text-destructive" role="alert">
              {errors.email.message}
            </p>
          ) : null}
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
    </AuthShell>
  )
}
