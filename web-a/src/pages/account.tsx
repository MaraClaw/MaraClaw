import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { PasswordField } from '@/components/ui/password-field'
import { useAuth } from '@/hooks/use-auth'
import { changePasswordRequest } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const schema = z
  .object({
    old_password: z.string().min(1, 'Enter your current password'),
    new_password: z
      .string()
      .min(6, 'Use at least 6 characters')
      .max(128, 'Password is too long'),
    confirm_password: z.string().min(1, 'Confirm your new password'),
  })
  .refine((data) => data.new_password === data.confirm_password, {
    message: 'Passwords do not match',
    path: ['confirm_password'],
  })
  .refine((data) => data.old_password !== data.new_password, {
    message: 'New password must be different from the current password',
    path: ['new_password'],
  })

type FormValues = z.infer<typeof schema>

export function AccountPage() {
  const { user, mustChangePassword, refreshUser } = useAuth()
  const formId = useId()
  const formErrorId = `${formId}-form-error`
  const [formError, setFormError] = useState<string | null>(null)

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      old_password: '',
      new_password: '',
      confirm_password: '',
    },
  })

  async function onSubmit(values: FormValues) {
    setFormError(null)
    try {
      const wasForced = mustChangePassword
      await changePasswordRequest({
        old_password: values.old_password,
        new_password: values.new_password,
      })
      reset()
      await refreshUser()
      toast.success(
        wasForced ? 'Password updated. You can use the admin console now.' : 'Password updated',
      )
    } catch (error) {
      if (error instanceof ApiError) {
        setFormError(formatApiDetail(error.detail) ?? 'Unable to update password.')
        return
      }
      setFormError('Unable to update password. Check your connection and try again.')
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">Account</Badge>
          {mustChangePassword ? <Badge variant="warning">Password change required</Badge> : null}
        </div>
        <h1 className="font-display text-3xl font-semibold tracking-tight">Account</h1>
        <p className="text-sm text-muted-foreground">
          Manage your admin credentials. Password changes apply to your global identity across
          organizations.
        </p>
      </div>

      {mustChangePassword ? (
        <div
          role="status"
          className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-950 dark:text-amber-100"
        >
          You signed in with a temporary password. Choose a new password before using the rest of
          the admin console.
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Profile</CardTitle>
          <CardDescription>Signed-in operator session.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <div className="flex flex-wrap justify-between gap-2 border-b border-border/70 pb-3">
            <span className="text-muted-foreground">Name</span>
            <span className="font-medium">{user?.display_name || '-'}</span>
          </div>
          <div className="flex flex-wrap justify-between gap-2 border-b border-border/70 pb-3">
            <span className="text-muted-foreground">Email</span>
            <span className="font-medium">{user?.email || '-'}</span>
          </div>
          <div className="flex flex-wrap justify-between gap-2">
            <span className="text-muted-foreground">Role</span>
            <span className="font-medium">{user?.role || '-'}</span>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Change password</CardTitle>
          <CardDescription>
            Enter your current password, then choose a new one (at least 6 characters).
          </CardDescription>
        </CardHeader>
        <CardContent>
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
            <PasswordField
              label="Current password"
              autoComplete="current-password"
              disabled={isSubmitting}
              error={errors.old_password?.message}
              {...register('old_password')}
            />
            <PasswordField
              label="New password"
              autoComplete="new-password"
              disabled={isSubmitting}
              error={errors.new_password?.message}
              {...register('new_password')}
            />
            <PasswordField
              label="Confirm new password"
              autoComplete="new-password"
              disabled={isSubmitting}
              error={errors.confirm_password?.message}
              {...register('confirm_password')}
            />

            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Loader2 className="size-4 animate-spin" aria-hidden />
                  Updating…
                </>
              ) : (
                'Update password'
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
