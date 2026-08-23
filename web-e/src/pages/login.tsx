import { zodResolver } from '@hookform/resolvers/zod'
import { Building2, Eye, EyeOff, Loader2, Lock, Mail, Sparkles, Users } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { SsoButtons } from '@/components/auth/sso-buttons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { ApiError, userFacingRequestError } from '@/lib/http'
import {
  isMultiTenantResponse,
  isNeedsVerificationDetail,
  isTokenResponse,
  type TenantChoice,
} from '@/lib/types/auth'
import { cn } from '@/lib/utils'

const loginSchema = z.object({
  login_identifier: z.string().trim().min(1, 'Enter your email').max(254, 'Email is too long'),
  password: z.string().min(1, 'Enter your password').max(128, 'Password is too long'),
})

type LoginFormValues = z.infer<typeof loginSchema>

const highlights = [
  {
    icon: Users,
    title: 'Join your company',
    body: 'Work email can place you in the right organization automatically.',
  },
  {
    icon: Sparkles,
    title: 'Digital employees',
    body: 'Talk to agents that already know your tools and workflows.',
  },
  {
    icon: Lock,
    title: 'Your session',
    body: 'Member credentials stay separate from the admin console.',
  },
]

export function LoginPage() {
  const { status, login, needsOrgConfirm } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const formId = useId()
  const identifierId = `${formId}-identifier`
  const passwordId = `${formId}-password`
  const formErrorId = `${formId}-form-error`
  const [showPassword, setShowPassword] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [tenantChoices, setTenantChoices] = useState<TenantChoice[] | null>(null)
  const [pendingPassword, setPendingPassword] = useState<string | null>(null)
  const [pendingIdentifier, setPendingIdentifier] = useState<string | null>(null)
  const [selectingTenantId, setSelectingTenantId] = useState<string | null>(null)
  const identifierRef = useRef<HTMLInputElement | null>(null)
  const passwordRef = useRef<HTMLInputElement | null>(null)

  const from =
    (location.state as { from?: { pathname?: string } } | null)?.from?.pathname &&
    (location.state as { from: { pathname: string } }).from.pathname !== '/login'
      ? (location.state as { from: { pathname: string } }).from.pathname
      : '/app'

  const {
    register,
    handleSubmit,
    setFocus,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { login_identifier: '', password: '' },
  })

  const identifierField = register('login_identifier')
  const passwordField = register('password')

  if (status === 'loading') {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Checking session…
        </div>
      </div>
    )
  }

  if (status === 'authenticated') {
    return <Navigate to={needsOrgConfirm ? '/join' : from} replace />
  }

  async function onSubmit(values: LoginFormValues) {
    setFormError(null)
    setTenantChoices(null)
    try {
      const result = await login({
        login_identifier: values.login_identifier.trim(),
        password: values.password,
      })
      if (isTokenResponse(result)) {
        toast.success('Signed in')
        navigate(result.needs_org_confirm ? '/join' : from, {
          replace: true,
          state: { suggested: result.suggested_org },
        })
        return
      }
      if (isMultiTenantResponse(result)) {
        setTenantChoices(result.tenants)
        setPendingPassword(values.password)
        setPendingIdentifier(result.login_identifier)
      }
    } catch (error) {
      if (error instanceof ApiError && isNeedsVerificationDetail(error.detail)) {
        navigate('/verify-email', {
          replace: true,
          state: { email: error.detail.email ?? values.login_identifier.trim() },
        })
        return
      }
      if (error instanceof ApiError && error.status === 401) {
        setFormError('Email or password is incorrect.')
      } else {
        setFormError(userFacingRequestError(error, 'Unable to sign in. Try again.'))
      }
      setFocus('password')
    }
  }

  async function chooseTenant(tenant: TenantChoice) {
    if (!pendingIdentifier || !pendingPassword || !tenant.tenant_id) {
      setFormError('This account needs a company. Create or join one first.')
      return
    }
    setSelectingTenantId(tenant.tenant_id)
    try {
      const result = await login({
        login_identifier: pendingIdentifier,
        password: pendingPassword,
        tenant_id: tenant.tenant_id,
      })
      if (isTokenResponse(result)) {
        toast.success(`Signed in to ${tenant.tenant_name}`)
        navigate(result.needs_org_confirm ? '/join' : from, { replace: true })
        return
      }
      setFormError('Unable to complete organization selection. Try again.')
    } catch (error) {
      setFormError(userFacingRequestError(error, 'Unable to sign in. Try again.'))
    } finally {
      setSelectingTenantId(null)
    }
  }

  const isBusy = isSubmitting || selectingTenantId !== null

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title={tenantChoices ? 'Choose organization' : 'Sign in'}
      description={
        tenantChoices
          ? 'This account belongs to more than one company. Select where to continue.'
          : 'Member accounts only. Platform operators use the admin console.'
      }
      brandTitle="Your digital teammates, ready when you are"
      brandBody="Sign in to join your company workspace and work with MaraClaw agents."
      highlights={highlights}
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

      {tenantChoices ? (
        <ul className="space-y-2" aria-label="Organizations">
          {tenantChoices.map((tenant) => {
            const key = tenant.tenant_id ?? tenant.tenant_slug
            const busy = selectingTenantId === tenant.tenant_id
            return (
              <li key={key}>
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={() => void chooseTenant(tenant)}
                  className={cn(
                    'flex w-full items-center gap-3 rounded-2xl border border-border bg-background px-3.5 py-3 text-left transition-[transform,background-color,border-color] duration-200',
                    'hover:border-primary/40 hover:bg-accent/40',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    'active:scale-[0.98] disabled:opacity-60',
                  )}
                >
                  <span className="flex size-10 shrink-0 items-center justify-center rounded-xl border border-border/70 bg-muted">
                    <Building2 className="size-4 text-muted-foreground" aria-hidden />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{tenant.tenant_name}</span>
                    {tenant.tenant_slug ? (
                      <span className="block truncate text-xs text-muted-foreground">
                        {tenant.tenant_slug}
                      </span>
                    ) : null}
                  </span>
                  {busy ? <Loader2 className="size-4 animate-spin text-muted-foreground" aria-hidden /> : null}
                </button>
              </li>
            )
          })}
        </ul>
      ) : (
        <form className="space-y-4" onSubmit={handleSubmit(onSubmit)} noValidate>
          <div className="space-y-2">
            <Label htmlFor={identifierId}>Email</Label>
            <div className="relative">
              <Mail
                className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                id={identifierId}
                type="email"
                autoComplete="username"
                placeholder="name@company.com"
                className="pl-10"
                disabled={isBusy}
                {...identifierField}
                ref={(el) => {
                  identifierField.ref(el)
                  identifierRef.current = el
                }}
              />
            </div>
            {errors.login_identifier ? (
              <p className="text-xs text-destructive">{errors.login_identifier.message}</p>
            ) : null}
          </div>

          <div className="space-y-2">
            <Label htmlFor={passwordId}>Password</Label>
            <div className="relative">
              <Lock
                className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                id={passwordId}
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                placeholder="••••••••"
                className="pr-11 pl-10"
                disabled={isBusy}
                {...passwordField}
                ref={(el) => {
                  passwordField.ref(el)
                  passwordRef.current = el
                }}
              />
              <button
                type="button"
                className="absolute top-1/2 right-2 flex size-9 -translate-y-1/2 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-foreground"
                onClick={() => setShowPassword((value) => !value)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? <EyeOff className="size-4" aria-hidden /> : <Eye className="size-4" aria-hidden />}
              </button>
            </div>
            {errors.password ? (
              <p className="text-xs text-destructive">{errors.password.message}</p>
            ) : null}
            <p className="text-right text-xs">
              <Link to="/forgot-password" className="font-medium text-primary underline-offset-4 hover:underline">
                Forgot password?
              </Link>
            </p>
          </div>

          <Button type="submit" className="h-11 w-full" disabled={isBusy}>
            {isSubmitting ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                Signing in…
              </>
            ) : (
              'Sign in'
            )}
          </Button>
          <div className="pt-2">
            <SsoButtons />
          </div>
        </form>
      )}

      <p className="mt-6 text-center text-sm text-muted-foreground">
        New here?{' '}
        <Link to="/register" className="font-medium text-primary underline-offset-4 hover:underline">
          Create an account
        </Link>
      </p>
    </AuthShell>
  )
}
