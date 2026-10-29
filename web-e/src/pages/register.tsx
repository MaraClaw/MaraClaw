import { zodResolver } from '@hookform/resolvers/zod'
import { Building2, Eye, EyeOff, Loader2, Lock, Mail, Sparkles } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { SsoButtons } from '@/components/auth/sso-buttons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { useAuth } from '@/hooks/use-auth'
import { checkDuplicate, fetchRegistrationConfig, registerRequest } from '@/lib/auth-api'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { isNeedsVerificationDetail } from '@/lib/types/auth'
import type { SuggestedOrg } from '@/lib/types/auth'

const registerSchema = z.object({
  email: z.string().trim().email('Enter a valid email').max(254),
  password: z.string().min(6, 'Use at least 6 characters').max(128),
  confirm_password: z.string().min(1, 'Confirm your password'),
  invitation_code: z.string().trim().max(128).optional(),
}).refine((data) => data.password === data.confirm_password, {
  message: 'Passwords do not match',
  path: ['confirm_password'],
})

type RegisterFormValues = z.infer<typeof registerSchema>

const duplicateEmailMessage = 'That email is already registered.'

const highlights = [
  {
    icon: Building2,
    title: 'Company match',
    body: 'A matching email domain submits a company join request, pending organization admin approval.',
  },
  {
    icon: Sparkles,
    title: 'Ready workspace',
    body: 'If your email domain does not match a company, you automatically join KABERs.',
  },
  {
    icon: Lock,
    title: 'Your account',
    body: 'Platform admin accounts cannot be created from this page.',
  },
]

export function RegisterPage() {
  const { status, owner, capture, isCurrent, applySession, needsOrgConfirm } = useAuth()
  const navigate = useNavigate()
  const formId = useId()
  const [showPassword, setShowPassword] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [inviteRequired, setInviteRequired] = useState(false)
  const [registrationDestination, setRegistrationDestination] = useState<{
    readonly to: string
    readonly state: { readonly email: string; readonly suggested: SuggestedOrg | null | undefined }
  } | null>(null)

  useEffect(() => {
    const expectedOwner = owner
    let cancelled = false
    void fetchRegistrationConfig(expectedOwner)
      .then((config) => {
        if (!cancelled && isCurrent(expectedOwner)) setInviteRequired(Boolean(config.invitation_code_required))
      })
      .catch(() => {
        if (!cancelled && isCurrent(expectedOwner)) setInviteRequired(false)
      })
    return () => { cancelled = true }
  }, [owner, isCurrent])

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterFormValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { email: '', password: '', confirm_password: '', invitation_code: '' },
  })

  if (status === 'authenticated') {
    return <Navigate to={registrationDestination?.to ?? (needsOrgConfirm ? '/join' : '/app')} state={registrationDestination?.state} replace />
  }

  async function onSubmit(values: RegisterFormValues) {
    const expectedOwner = owner
    if (!isCurrent(expectedOwner)) return
    setFormError(null)
    try {
      const result = await registerRequest({
        email: values.email,
        password: values.password,
        invitation_code: values.invitation_code?.trim() || undefined,
      }, undefined, expectedOwner)
      if (!isCurrent(expectedOwner)) return
      setRegistrationDestination({
        to: !result.user.email_verified ? '/verify-email'
          : result.admission_request ? `/app/admissions/${encodeURIComponent(result.admission_request.id)}`
          : result.needs_org_confirm ? '/join' : '/app',
        state: { email: values.email, suggested: result.suggested_org },
      })
      if (!applySession(expectedOwner, result)) return
      const installedOwner = capture()
      if (!isCurrent(installedOwner)) return
      toast.success('Account created')
    } catch (error) {
      if (!isCurrent(expectedOwner)) return
      if (error instanceof ApiError && isNeedsVerificationDetail(error.detail)) {
        navigate('/verify-email', { replace: true, state: { email: values.email } })
        return
      }
      setFormError(userFacingRequestError(error, 'Unable to create an account. Try again.'))
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title="Create your account"
      description="At signup, a matching email domain submits a company join request, pending organization admin approval. Without a match, you automatically join KABERs. An invitation code submits a company admission request for review."
      brandTitle="Start working with digital employees"
      brandBody="Sign up to request company membership with a matching email domain, subject to organization admin approval. Without a match, start in KABERs."
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

      <form className="space-y-4" onSubmit={handleSubmit(onSubmit)} noValidate>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-email`}>Email</Label>
          <div className="relative">
            <Mail className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id={`${formId}-email`}
              type="email"
              autoComplete="email"
              className="pl-10"
              {...register('email', {
                onBlur: (event) => {
                  const expectedOwner = owner
                  if (!isCurrent(expectedOwner)) return
                  const email = event.target.value.trim()
                  if (!email) {
                    setFormError((current) => (current === duplicateEmailMessage ? null : current))
                    return
                  }
                  void checkDuplicate({ email }, expectedOwner)
                    .then((result) => {
                      if (!isCurrent(expectedOwner)) return
                      setFormError((current) => {
                        if (result.email_exists) return duplicateEmailMessage
                        return current === duplicateEmailMessage ? null : current
                      })
                    })
                    .catch(() => undefined)
                },
              })}
            />
          </div>
          {errors.email ? <p className="text-xs text-destructive">{errors.email.message}</p> : null}
        </div>

        <div className="space-y-2">
          <Label htmlFor={`${formId}-password`}>Password</Label>
          <div className="relative">
            <Lock className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id={`${formId}-password`}
              type={showPassword ? 'text' : 'password'}
              autoComplete="new-password"
              className="pr-11 pl-10"
              {...register('password')}
            />
            <button
              type="button"
              className="absolute top-1/2 right-2 flex size-9 -translate-y-1/2 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted"
              onClick={() => setShowPassword((value) => !value)}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
            >
              {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
          {errors.password ? <p className="text-xs text-destructive">{errors.password.message}</p> : null}
        </div>

        <PasswordField
          label="Confirm password"
          autoComplete="new-password"
          disabled={isSubmitting}
          error={errors.confirm_password?.message}
          {...register('confirm_password')}
        />

        <div className="space-y-2">
          <Label htmlFor={`${formId}-invite`}>Invitation code{inviteRequired ? '' : ' (optional)'}</Label>
          <Input
            id={`${formId}-invite`}
            placeholder={inviteRequired ? 'Required' : 'Optional'}
            autoComplete="off"
            {...register('invitation_code', {
              required: inviteRequired ? 'An invitation code is required' : false,
            })}
          />
          {errors.invitation_code ? (
            <p className="text-xs text-destructive">{errors.invitation_code.message}</p>
          ) : null}
        </div>

        <Button type="submit" className="h-11 w-full" disabled={isSubmitting}>
          {isSubmitting ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Creating…
            </>
          ) : (
            'Create account'
          )}
        </Button>
        <div className="pt-2">
          <SsoButtons />
        </div>
      </form>

      <p className="mt-6 text-center text-sm text-muted-foreground">
        Already have an account?{' '}
        <Link to="/login" className="font-medium text-primary underline-offset-4 hover:underline">
          Sign in
        </Link>
      </p>
    </AuthShell>
  )
}
