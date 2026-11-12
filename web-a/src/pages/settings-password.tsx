import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { PasswordField } from '@/components/ui/password-field'
import { useAuth } from '@/hooks/use-auth'
import { changePasswordRequest } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'
import { useTranslation } from '@/hooks/use-translation'
import { useLocalizedMessage } from '@/hooks/use-localized-message'
import { message } from '@/lib/i18n'

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

export function SettingsPassword() {
  const { t } = useTranslation()
  const { owner, mustChangePassword, refreshReplacement, isCurrent } = useAuth()
  const [formError, setFormError] = useLocalizedMessage()

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
    const expectedOwner = owner
    if (!isCurrent(expectedOwner)) return
    setFormError(null)
    try {
      const wasForced = mustChangePassword
      const result = await changePasswordRequest({
        old_password: values.old_password,
        new_password: values.new_password,
      }, expectedOwner.signal)
      const installedOwner = await refreshReplacement(expectedOwner, result.access_token)
      if (!installedOwner || !isCurrent(installedOwner)) return
      reset()
      toast.success(
        t(wasForced ? 'Password updated. You can use the admin console now.' : 'Password updated'),
      )
    } catch (error) {
      if (!isCurrent(expectedOwner)) return
      if (error instanceof ApiError) {
        setFormError(formatApiDetail(error.detail) ?? message('Unable to update password.'))
        return
      }
      setFormError(message('Unable to update password. Check your connection and try again.'))
    }
  }

  return (
    <Card id="change-password" className="scroll-mt-6">
      <CardHeader>
        <CardTitle>{t('Change password')}</CardTitle>
        <CardDescription>
          {t('Enter your current password, then choose a new one (at least 6 characters). Password changes apply to your global identity across organizations.')}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {formError ? (
          <ErrorToast message={formError} />
        ) : null}

        <form
          className="space-y-4"
          onSubmit={handleSubmit(onSubmit)}
          noValidate
        >
          <PasswordField
            label={t('Current password')}
            autoComplete="current-password"
            disabled={isSubmitting}
            error={errors.old_password?.message}
            {...register('old_password')}
          />
          <PasswordField
            label={t('New password')}
            autoComplete="new-password"
            disabled={isSubmitting}
            error={errors.new_password?.message}
            {...register('new_password')}
          />
          <PasswordField
            label={t('Confirm new password')}
            autoComplete="new-password"
            disabled={isSubmitting}
            error={errors.confirm_password?.message}
            {...register('confirm_password')}
          />

          <Button type="submit" disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                {t('Updating…')}
              </>
            ) : (
              t('Update password')
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
