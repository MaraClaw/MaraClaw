import { zodResolver } from '@hookform/resolvers/zod'
import { motion, useReducedMotion } from 'framer-motion'
import {
  ArrowLeft,
  Building2,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
  ShieldCheck,
} from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { ThemeToggle } from '@/components/theme-toggle'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  isMultiTenantResponse,
  isTokenResponse,
  type NeedsVerificationDetail,
  type TenantChoice,
} from '@/lib/types/auth'
import { cn } from '@/lib/utils'

const loginSchema = z.object({
  login_identifier: z
    .string()
    .trim()
    .min(1, 'Enter your email, phone, or username')
    .max(254, 'Identifier is too long'),
  password: z.string().min(1, 'Enter your password').max(128, 'Password is too long'),
})

type LoginFormValues = z.infer<typeof loginSchema>

const easeOut = [0.23, 1, 0.32, 1] as const

function parseLoginError(error: unknown): {
  formMessage: string
  field?: keyof LoginFormValues
  needsVerification?: NeedsVerificationDetail
} {
  if (!(error instanceof ApiError)) {
    return { formMessage: 'Unable to sign in. Check your connection and try again.' }
  }

  const detail = error.detail

  if (detail && typeof detail === 'object' && 'needs_verification' in detail) {
    const v = detail as NeedsVerificationDetail
    return {
      formMessage: v.message ?? 'Please verify your email to continue.',
      needsVerification: v,
    }
  }

  if (error.status === 401) {
    return {
      formMessage: 'Email or password is incorrect.',
      field: 'password',
    }
  }

  if (error.status === 403) {
    const message = formatApiDetail(detail) ?? 'Access denied for this account.'
    return { formMessage: message }
  }

  if (error.status === 404) {
    return {
      formMessage: formatApiDetail(detail) ?? 'No organization is linked to this account.',
    }
  }

  return {
    formMessage: formatApiDetail(detail) ?? 'Unable to sign in. Try again.',
  }
}

export function LoginPage() {
  const { status, login, isAdmin } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const reduceMotion = useReducedMotion()
  const formId = useId()
  const identifierId = `${formId}-identifier`
  const passwordId = `${formId}-password`
  const formErrorId = `${formId}-form-error`
  const identifierErrorId = `${formId}-identifier-error`
  const passwordErrorId = `${formId}-password-error`

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
      : '/'

  const {
    register,
    handleSubmit,
    setError,
    setFocus,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      login_identifier: '',
      password: '',
    },
    mode: 'onSubmit',
  })

  const identifierField = register('login_identifier')
  const passwordField = register('password')

  useEffect(() => {
    if (status === 'anonymous' && !tenantChoices) {
      identifierRef.current?.focus()
    }
  }, [status, tenantChoices])

  const motionProps = useMemo(
    () =>
      reduceMotion
        ? { initial: false as const, animate: { opacity: 1 } }
        : {
            initial: { opacity: 0, y: 10 },
            animate: { opacity: 1, y: 0 },
            transition: { duration: 0.35, ease: easeOut },
          },
    [reduceMotion],
  )

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

  if (status === 'authenticated' && isAdmin) {
    return <Navigate to={from} replace />
  }

  async function onSubmit(values: LoginFormValues) {
    setFormError(null)
    setTenantChoices(null)
    setPendingPassword(null)
    setPendingIdentifier(null)

    try {
      const result = await login({
        login_identifier: values.login_identifier.trim(),
        password: values.password,
      })

      if (isTokenResponse(result)) {
        toast.success('Signed in')
        navigate(from, { replace: true })
        return
      }

      if (isMultiTenantResponse(result)) {
        setTenantChoices(result.tenants)
        setPendingPassword(values.password)
        setPendingIdentifier(result.login_identifier)
        return
      }
    } catch (error) {
      const parsed = parseLoginError(error)
      setFormError(parsed.formMessage)
      if (parsed.field) {
        setError(parsed.field, { type: 'server', message: parsed.formMessage })
        setFocus(parsed.field)
      } else if (errors.login_identifier) {
        setFocus('login_identifier')
      } else {
        setFocus('password')
      }
    }
  }

  async function chooseTenant(tenant: TenantChoice) {
    if (!pendingIdentifier || !pendingPassword) return
    if (!tenant.tenant_id) {
      setFormError('This account needs a company. Create or join one first, then sign in again.')
      return
    }

    setFormError(null)
    setSelectingTenantId(tenant.tenant_id)

    try {
      const result = await login({
        login_identifier: pendingIdentifier,
        password: pendingPassword,
        tenant_id: tenant.tenant_id,
      })

      if (isTokenResponse(result)) {
        toast.success(`Signed in to ${tenant.tenant_name}`)
        navigate(from, { replace: true })
        return
      }

      setFormError('Unable to complete organization selection. Try again.')
    } catch (error) {
      const parsed = parseLoginError(error)
      setFormError(parsed.formMessage)
    } finally {
      setSelectingTenantId(null)
    }
  }

  function backToCredentials() {
    setTenantChoices(null)
    setPendingPassword(null)
    setPendingIdentifier(null)
    setFormError(null)
    setTimeout(() => identifierRef.current?.focus(), 0)
  }

  const isBusy = isSubmitting || selectingTenantId !== null

  return (
    <div className="relative min-h-svh overflow-hidden bg-background">
      {/* Ambient background */}
      <div className="pointer-events-none absolute inset-0" aria-hidden>
        <div className="absolute -left-24 top-[-10%] size-[28rem] rounded-full bg-primary/15 blur-3xl dark:bg-primary/10" />
        <div className="absolute -right-20 bottom-[-15%] size-[32rem] rounded-full bg-[oklch(0.7_0.08_220/0.14)] blur-3xl dark:bg-[oklch(0.45_0.08_220/0.18)]" />
        <div
          className="absolute inset-0 opacity-[0.35] dark:opacity-[0.2]"
          style={{
            backgroundImage:
              'radial-gradient(circle at 1px 1px, oklch(0.35 0.02 45 / 0.12) 1px, transparent 0)',
            backgroundSize: '24px 24px',
          }}
        />
      </div>

      <div className="relative z-10 flex min-h-svh flex-col">
        <header className="flex items-center justify-between px-5 py-4 md:px-8">
          <div className="flex items-center gap-2.5">
            <MaraClawLogo className="size-[3.375rem]" />
            <div className="min-w-0">
              <p className="font-display text-sm font-semibold leading-tight">MaraClaw</p>
              <p className="text-xs text-muted-foreground">Admin console</p>
            </div>
          </div>
          <ThemeToggle />
        </header>

        <main className="flex flex-1 items-center justify-center px-4 py-8 md:px-8 md:py-12">
          <div className="grid w-full max-w-5xl gap-6 lg:grid-cols-[1.05fr_0.95fr] lg:gap-8">
            {/* Brand panel */}
            <motion.section
              {...motionProps}
              className="relative hidden overflow-hidden rounded-[1.75rem] border border-border/70 bg-card/70 p-8 shadow-elevated backdrop-blur-xl lg:flex lg:flex-col lg:justify-between"
              aria-label="Product introduction"
            >
              <div
                className="pointer-events-none absolute inset-0 opacity-90"
                style={{
                  background:
                    'linear-gradient(145deg, oklch(0.97 0.02 55 / 0.9) 0%, oklch(0.99 0.01 80 / 0.55) 45%, oklch(0.94 0.03 210 / 0.35) 100%)',
                }}
                aria-hidden
              />
              <div
                className="pointer-events-none absolute inset-0 dark:opacity-100"
                style={{
                  background:
                    'linear-gradient(145deg, oklch(0.24 0.04 42 / 0.75) 0%, oklch(0.18 0.02 40 / 0.4) 55%, oklch(0.17 0.03 220 / 0.45) 100%)',
                }}
                aria-hidden
              />

              <div className="relative z-10 flex flex-col gap-6">
                <Badge variant="secondary" className="w-fit bg-background/70">
                  Operators only
                </Badge>
                <div className="space-y-3">
                  <h1 className="font-display text-3xl font-semibold tracking-tight text-balance md:text-4xl">
                    Run your digital workforce with confidence
                  </h1>
                  <p className="max-w-md text-sm leading-relaxed text-muted-foreground md:text-base">
                    Manage companies, users, tools, and platform policy from one console. Built for
                    platform admins and organization admins.
                  </p>
                </div>
              </div>

              <ul className="relative z-10 mt-10 grid gap-3" aria-label="Admin capabilities">
                {[
                  {
                    icon: ShieldCheck,
                    title: 'Governed access',
                    body: 'Role-aware controls for platform and tenant operators.',
                  },
                  {
                    icon: Building2,
                    title: 'Company operations',
                    body: 'Fleet, quotas, SSO, and enterprise settings in one place.',
                  },
                  {
                    icon: Lock,
                    title: 'Secure session',
                    body: 'Signed-in traffic stays on your engine API with admin scopes.',
                  },
                ].map((item, index) => (
                  <motion.li
                    key={item.title}
                    initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={
                      reduceMotion
                        ? undefined
                        : { delay: 0.08 + index * 0.07, duration: 0.3, ease: easeOut }
                    }
                    className="flex gap-3 rounded-2xl border border-border/60 bg-background/55 p-3.5 backdrop-blur-md"
                  >
                    <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/12 text-primary">
                      <item.icon className="size-4" aria-hidden />
                    </span>
                    <span>
                      <span className="block text-sm font-medium">{item.title}</span>
                      <span className="mt-0.5 block text-xs leading-relaxed text-muted-foreground">
                        {item.body}
                      </span>
                    </span>
                  </motion.li>
                ))}
              </ul>
            </motion.section>

            {/* Auth card */}
            <motion.section
              {...(reduceMotion
                ? { initial: false as const }
                : {
                    initial: { opacity: 0, y: 12 },
                    animate: { opacity: 1, y: 0 },
                    transition: { duration: 0.4, delay: 0.05, ease: easeOut },
                  })}
              className="mx-auto w-full max-w-md"
              aria-labelledby={`${formId}-heading`}
            >
              <div className="rounded-[1.75rem] border border-border/80 bg-card/90 p-6 shadow-elevated backdrop-blur-xl sm:p-8">
                <div className="mb-6 space-y-2">
                  <div className="mb-4 flex items-center gap-2 lg:hidden">
                    <MaraClawLogo className="size-[3.75rem]" />
                    <Badge variant="outline">Admin</Badge>
                  </div>
                  <h2
                    id={`${formId}-heading`}
                    className="font-display text-2xl font-semibold tracking-tight"
                  >
                    {tenantChoices ? 'Choose organization' : 'Sign in'}
                  </h2>
                  <p className="text-sm text-muted-foreground">
                    {tenantChoices
                      ? 'This account belongs to more than one company. Select where to continue.'
                      : 'Use your MaraClaw admin credentials to open the operator console.'}
                  </p>
                </div>

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
                  <div className="space-y-3">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="-ml-2 mb-1"
                      onClick={backToCredentials}
                      disabled={isBusy}
                    >
                      <ArrowLeft className="size-4" aria-hidden />
                      Back to credentials
                    </Button>

                    <ul className="space-y-2" aria-label="Organizations">
                      {tenantChoices.map((tenant) => {
                        const key = tenant.tenant_id ?? tenant.tenant_slug ?? tenant.tenant_name
                        const busy = selectingTenantId === tenant.tenant_id
                        return (
                          <li key={key}>
                            <button
                              type="button"
                              disabled={isBusy}
                              onClick={() => void chooseTenant(tenant)}
                              className={cn(
                                'flex w-full items-center gap-3 rounded-2xl border border-border bg-background px-3.5 py-3 text-left transition-[transform,background-color,border-color,box-shadow] duration-200',
                                'hover:border-primary/40 hover:bg-accent/40',
                                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card',
                                'active:scale-[0.98] disabled:opacity-60',
                              )}
                            >
                              <span className="flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-xl border border-border/70 bg-muted">
                                {tenant.logo_url ? (
                                  <img
                                    src={tenant.logo_url}
                                    alt=""
                                    className="size-full object-cover"
                                  />
                                ) : (
                                  <Building2 className="size-4 text-muted-foreground" aria-hidden />
                                )}
                              </span>
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-sm font-medium">
                                  {tenant.tenant_name}
                                </span>
                                {tenant.tenant_slug ? (
                                  <span className="block truncate text-xs text-muted-foreground">
                                    {tenant.tenant_slug}
                                  </span>
                                ) : null}
                              </span>
                              {busy ? (
                                <Loader2 className="size-4 animate-spin text-muted-foreground" aria-hidden />
                              ) : null}
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                  </div>
                ) : (
                  <form
                    className="space-y-4"
                    onSubmit={handleSubmit(onSubmit)}
                    noValidate
                    aria-describedby={formError ? formErrorId : undefined}
                  >
                    <div className="space-y-2">
                      <Label htmlFor={identifierId}>Email, phone, or username</Label>
                      <div className="relative">
                        <Mail
                          className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
                          aria-hidden
                        />
                        <Input
                          id={identifierId}
                          type="text"
                          autoComplete="username"
                          inputMode="email"
                          spellCheck={false}
                          placeholder="name@company.com"
                          className="pl-10"
                          aria-invalid={errors.login_identifier ? true : undefined}
                          aria-describedby={
                            errors.login_identifier ? identifierErrorId : undefined
                          }
                          disabled={isBusy}
                          {...identifierField}
                          ref={(el) => {
                            identifierField.ref(el)
                            identifierRef.current = el
                          }}
                        />
                      </div>
                      {errors.login_identifier ? (
                        <p id={identifierErrorId} className="text-xs text-destructive" role="alert">
                          {errors.login_identifier.message}
                        </p>
                      ) : null}
                    </div>

                    <div className="space-y-2">
                      <div className="flex items-center justify-between gap-3">
                        <Label htmlFor={passwordId}>Password</Label>
                        <Link
                          to="/forgot-password"
                          className="text-xs font-medium text-primary underline-offset-4 hover:underline"
                        >
                          Forgot password?
                        </Link>
                      </div>
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
                          aria-invalid={errors.password ? true : undefined}
                          aria-describedby={errors.password ? passwordErrorId : undefined}
                          disabled={isBusy}
                          {...passwordField}
                          ref={(el) => {
                            passwordField.ref(el)
                            passwordRef.current = el
                          }}
                        />
                        <button
                          type="button"
                          className="absolute top-1/2 right-2 flex size-9 -translate-y-1/2 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          onClick={() => setShowPassword((v) => !v)}
                          aria-label={showPassword ? 'Hide password' : 'Show password'}
                          disabled={isBusy}
                        >
                          {showPassword ? (
                            <EyeOff className="size-4" aria-hidden />
                          ) : (
                            <Eye className="size-4" aria-hidden />
                          )}
                        </button>
                      </div>
                      {errors.password ? (
                        <p id={passwordErrorId} className="text-xs text-destructive" role="alert">
                          {errors.password.message}
                        </p>
                      ) : null}
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
                  </form>
                )}

                <p className="mt-6 text-center text-xs leading-relaxed text-muted-foreground">
                  Platform admins and organization admins only.
                </p>
              </div>
            </motion.section>
          </div>
        </main>
      </div>
    </div>
  )
}
