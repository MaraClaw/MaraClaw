import { useTranslation } from '@/hooks/use-translation'
import { ValidationMessage } from '@/components/validation-message'
import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeft, Loader2, Mail } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'
import { toast } from 'sonner'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { memberAuthHighlights } from '@/lib/auth-highlights'
import { forgotPasswordRequest } from '@/lib/auth-api'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { authSession } from '@/lib/auth-session'

const schema = z.object({
  email: z.string().trim().min(1, 'Enter your email').email('Enter a valid email').max(254, 'Email is too long'),
})

type FormValues = z.infer<typeof schema>

export function ForgotPasswordPage() {
  const { t: translate } = useTranslation()
  const formId = useId()
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
    const owner = authSession.capture()
    try {
      await forgotPasswordRequest(values.email.trim().toLowerCase())
      if (authSession.isCurrent(owner)) setSent(true)
    } catch (error) {
      if (authSession.isCurrent(owner)) toast.error(userFacingRequestError(error,
        error instanceof ApiError && error.status === 400
          ? translate("Password reset is currently unavailable. Contact your operator.") : translate("Unable to send reset email. Try again.")))
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title={sent ? translate("Check your email") : translate("Reset password")}
      description={
        sent
          ? translate("If an account with that email exists, a reset link has been sent.")
          : translate("Enter the email for your member account. We will send a reset link if it matches.")
      }
      brandTitle={translate("Get back into your workspace")}
      brandBody={translate("Reset links from this site stay on MaraClaw member sign-in, not the admin console.")}
      highlights={memberAuthHighlights}
    >
      {sent ? (
        <div className="space-y-4">
          <p className="rounded-xl border border-border/80 bg-muted/40 px-3.5 py-3 text-sm text-muted-foreground">
            {translate("Sent to")}{' '}<span className="font-medium text-foreground">{getValues('email')}</span>{translate(". Check spam if you do not see it.")}</p>
          <Button asChild variant="outline" className="h-11 w-full">
            <Link to="/login">
              <ArrowLeft className="size-4" aria-hidden />
              {translate("Return to sign in")}</Link>
          </Button>
        </div>
      ) : (
        <form className="space-y-4" onSubmit={handleSubmit(onSubmit)} noValidate>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-email`}>{translate("Email")}</Label>
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
            {errors.email ? <p className="text-xs text-destructive"><ValidationMessage message={errors.email.message} /></p> : null}
          </div>
          <Button type="submit" className="h-11 w-full" disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                {translate("Sending link…")}</>
            ) : (
              translate("Send reset link")
            )}
          </Button>
        </form>
      )}

      <p className="mt-6 text-center text-sm text-muted-foreground">
        <Link to="/login" className="font-medium text-primary underline-offset-4 hover:underline">
          {translate("Back to sign in")}</Link>
      </p>
    </AuthShell>
  )
}
