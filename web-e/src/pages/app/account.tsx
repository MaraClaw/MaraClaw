import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { useAuth } from '@/hooks/use-auth'
import { authorizeProvider, changePasswordRequest, listAuthProviders, unbindProvider, updateCurrentUser } from '@/lib/auth-api'
import { SSO_INTENT_KEY, SSO_PROVIDER_KEY, SSO_STATE_KEY } from '@/lib/sso-storage'
import { ApiError, formatApiDetail } from '@/lib/http'
import type { AuthProvider } from '@/lib/types/auth'

const passwordSchema = z
  .object({
    old_password: z.string().min(1, 'Enter your current password'),
    new_password: z.string().min(6, 'Use at least 6 characters').max(128),
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

const profileSchema = z.object({
  display_name: z.string().trim().min(1).max(100),
  username: z.string().trim().max(64).optional(),
})

type PasswordValues = z.infer<typeof passwordSchema>
type ProfileValues = z.infer<typeof profileSchema>

export function AccountPage() {
  const { user, mustChangePassword, refreshUser } = useAuth()
  const formId = useId()
  const [passwordError, setPasswordError] = useState<string | null>(null)
  const [profileError, setProfileError] = useState<string | null>(null)
  const [providers, setProviders] = useState<AuthProvider[]>([])

  useEffect(() => {
    void listAuthProviders()
      .then((rows) => setProviders(rows.filter((row) => row.is_active)))
      .catch(() => setProviders([]))
  }, [])

  const passwordForm = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { old_password: '', new_password: '', confirm_password: '' },
  })
  const profileForm = useForm<ProfileValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: { display_name: user?.display_name ?? '', username: user?.username ?? '' },
  })

  async function onPassword(values: PasswordValues) {
    setPasswordError(null)
    try {
      await changePasswordRequest({
        old_password: values.old_password,
        new_password: values.new_password,
      })
      passwordForm.reset()
      await refreshUser()
      toast.success(mustChangePassword ? 'Password updated. You can use the workspace now.' : 'Password updated')
    } catch (error) {
      setPasswordError(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Unable to update password.')
    }
  }

  async function onProfile(values: ProfileValues) {
    setProfileError(null)
    try {
      await updateCurrentUser({
        display_name: values.display_name,
        username: values.username || undefined,
      })
      await refreshUser()
      toast.success('Profile updated')
    } catch (error) {
      setProfileError(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Unable to update profile.')
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Account</h1>
        <p className="text-sm text-muted-foreground">{user?.email}</p>
      </div>

      {mustChangePassword ? (
        <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive">
          You must choose a new password before using the workspace.
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Profile</CardTitle>
          <CardDescription>How teammates see you in this organization.</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={profileForm.handleSubmit(onProfile)}>
            {profileError ? <p className="text-sm text-destructive">{profileError}</p> : null}
            <div className="space-y-2">
              <Label htmlFor={`${formId}-name`}>Display name</Label>
              <Input id={`${formId}-name`} {...profileForm.register('display_name')} />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-user`}>Username</Label>
              <Input id={`${formId}-user`} {...profileForm.register('username')} />
            </div>
            <Button type="submit" disabled={profileForm.formState.isSubmitting}>
              Save profile
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Password</CardTitle>
          <CardDescription>The new password must differ from the current one.</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={passwordForm.handleSubmit(onPassword)}>
            {passwordError ? <p className="text-sm text-destructive">{passwordError}</p> : null}
            <PasswordField label="Current password" autoComplete="current-password" error={passwordForm.formState.errors.old_password?.message} {...passwordForm.register('old_password')} />
            <PasswordField label="New password" autoComplete="new-password" error={passwordForm.formState.errors.new_password?.message} {...passwordForm.register('new_password')} />
            <PasswordField label="Confirm new password" autoComplete="new-password" error={passwordForm.formState.errors.confirm_password?.message} {...passwordForm.register('confirm_password')} />
            <Button type="submit" disabled={passwordForm.formState.isSubmitting}>
              {passwordForm.formState.isSubmitting ? <Loader2 className="size-4 animate-spin" /> : 'Update password'}
            </Button>
          </form>
        </CardContent>
      </Card>

      {providers.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Connected sign-in</CardTitle>
            <CardDescription>Link a company SSO provider to this account, or remove it.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {providers.map((provider) => (
              <div key={provider.id} className="flex items-center justify-between gap-2 rounded-xl border border-border px-3 py-2">
                <span className="text-sm">{provider.name || provider.provider_type}</span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      const state = crypto.randomUUID()
                      sessionStorage.setItem(SSO_STATE_KEY, state)
                      sessionStorage.setItem(SSO_PROVIDER_KEY, provider.provider_type)
                      sessionStorage.setItem(SSO_INTENT_KEY, 'bind')
                      const redirectUri = `${window.location.origin}/sso/callback`
                      void authorizeProvider(provider.provider_type, redirectUri, state).then((result) => {
                        window.location.assign(result.authorization_url)
                      })
                    }}
                  >
                    Connect
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      void unbindProvider(provider.provider_type).then(() => toast.success('Unlinked'))
                    }
                  >
                    Remove
                  </Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
