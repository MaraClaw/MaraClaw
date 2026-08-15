import { zodResolver } from '@hookform/resolvers/zod'
import { Building2, Eye, EyeOff, Loader2, Lock, Mail, Sparkles, UserRound } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { registerRequest } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const registerSchema = z.object({
  email: z.string().trim().email('Enter a valid email').max(254),
  username: z.string().trim().max(64).optional(),
  password: z.string().min(6, 'Use at least 6 characters').max(128),
  invitation_code: z.string().trim().max(128).optional(),
})

type RegisterFormValues = z.infer<typeof registerSchema>

const highlights = [
  {
    icon: Building2,
    title: 'Company match',
    body: 'If your work email is claimed, we will ask you to join that company.',
  },
  {
    icon: Sparkles,
    title: 'Ready workspace',
    body: 'Otherwise you land in OpenClaw, the default member organization.',
  },
  {
    icon: Lock,
    title: 'Your account',
    body: 'Platform admin accounts cannot be created from this page.',
  },
]

export function RegisterPage() {
  const { status, applySession, needsOrgConfirm } = useAuth()
  const navigate = useNavigate()
  const formId = useId()
  const [showPassword, setShowPassword] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterFormValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { email: '', username: '', password: '', invitation_code: '' },
  })

  if (status === 'authenticated') {
    return <Navigate to={needsOrgConfirm ? '/join' : '/app'} replace />
  }

  async function onSubmit(values: RegisterFormValues) {
    setFormError(null)
    try {
      const result = await registerRequest({
        email: values.email,
        username: values.username?.trim() || values.email.split('@')[0] || 'member',
        password: values.password,
        invitation_code: values.invitation_code?.trim() || undefined,
      })
      applySession(result)
      toast.success('Account created')
      navigate(result.needs_org_confirm ? '/join' : '/app', {
        replace: true,
        state: { suggested: result.suggested_org },
      })
    } catch (error) {
      setFormError(
        error instanceof ApiError
          ? (formatApiDetail(error.detail) ?? error.message)
          : 'Registration failed. Try again.',
      )
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title="Create your account"
      description="If your work email matches a company, we will ask you to join it. Otherwise you join OpenClaw."
      brandTitle="Start working with digital employees"
      brandBody="One account gets you into your company workspace - or OpenClaw if you are joining on your own."
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
            <Input id={`${formId}-email`} type="email" autoComplete="email" className="pl-10" {...register('email')} />
          </div>
          {errors.email ? <p className="text-xs text-destructive">{errors.email.message}</p> : null}
        </div>

        <div className="space-y-2">
          <Label htmlFor={`${formId}-username`}>Username</Label>
          <div className="relative">
            <UserRound className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id={`${formId}-username`}
              autoComplete="username"
              placeholder="Optional"
              className="pl-10"
              {...register('username')}
            />
          </div>
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

        <div className="space-y-2">
          <Label htmlFor={`${formId}-invite`}>Invitation code</Label>
          <Input
            id={`${formId}-invite`}
            placeholder="Optional"
            autoComplete="off"
            {...register('invitation_code')}
          />
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
