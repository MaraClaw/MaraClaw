import { useTranslation } from '@/hooks/use-translation'
import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { useId, useMemo } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { PasswordField } from '@/components/ui/password-field'
import { memberAuthHighlights } from '@/lib/auth-highlights'
import { resetPasswordRequest } from '@/lib/auth-api'
import { userFacingRequestError } from '@/lib/http'
import { authSession } from '@/lib/auth-session'

const schema = z
  .object({
    new_password: z.string().min(6, 'Use at least 6 characters').max(128, 'Password is too long'),
    confirm_password: z.string().min(1, 'Confirm your new password'),
  })
  .refine((data) => data.new_password === data.confirm_password, {
    message: 'Passwords do not match',
    path: ['confirm_password'],
  })

type FormValues = z.infer<typeof schema>

export function ResetPasswordPage() {
  const { t: translate } = useTranslation()
  const formId = useId()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const token = useMemo(() => searchParams.get('token')?.trim() ?? '', [searchParams])
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { new_password: '', confirm_password: '' },
  })

  const tokenMissing = token.length < 20

  async function onSubmit(values: FormValues) {
    if (tokenMissing) return
    const owner = authSession.capture()
    try {
      await resetPasswordRequest({ token, new_password: values.new_password })
      if (!authSession.isCurrent(owner)) return
      toast.success(translate("Password updated. Sign in with your new password."))
      navigate('/login', { replace: true })
    } catch (error) {
      if (authSession.isCurrent(owner)) toast.error(userFacingRequestError(error, translate("Unable to update password. Try again.")))
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title={tokenMissing ? translate("Invalid reset link") : translate("Choose a new password")}
      description={
        tokenMissing
          ? translate("This password reset link is missing or incomplete.")
          : translate("Enter a new password for your member account. You will sign in again afterward.")
      }
      brandTitle={translate("New password, same workspace")}
      brandBody={translate("After you reset, sign in here — operators use the admin console.")}
      highlights={memberAuthHighlights}
    >
      {tokenMissing ? (
        <div className="space-y-4">
          <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive">
            {translate("Open the full link from your email, or request a new password reset.")}</div>
          <Button asChild className="h-11 w-full">
            <Link to="/forgot-password">{translate("Request a new link")}</Link>
          </Button>
        </div>
      ) : (
        <form className="space-y-4" onSubmit={handleSubmit(onSubmit)} noValidate>
          <PasswordField
            label={translate("New password")}
            autoComplete="new-password"
            disabled={isSubmitting}
            error={errors.new_password?.message}
            {...register('new_password')}
          />
          <PasswordField
            label={translate("Confirm new password")}
            autoComplete="new-password"
            disabled={isSubmitting}
            error={errors.confirm_password?.message}
            {...register('confirm_password')}
          />
          <Button type="submit" className="h-11 w-full" disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                {translate("Updating password…")}</>
            ) : (
              translate("Update password")
            )}
          </Button>
        </form>
      )}
      <p className="mt-6 text-center text-sm text-muted-foreground">
        <Link to="/login" className="inline-flex items-center gap-1.5 font-medium text-primary underline-offset-4 hover:underline">
          <ArrowLeft className="size-3.5" aria-hidden />
          {translate("Back to sign in")}</Link>
      </p>
    </AuthShell>
  )
}
