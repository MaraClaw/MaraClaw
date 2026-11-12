import { useTranslation } from '@/hooks/use-translation'
import { LanguageSelector } from '@/components/language-selector'
import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { Loader2, LogOut } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { useAuth } from '@/hooks/use-auth'
import { authorizeProvider, changePasswordRequest, fetchMyTenants, listAuthProviders, switchTenant, unbindProvider, updateCurrentUser } from '@/lib/auth-api'
import { storeSsoTransaction } from '@/lib/sso-storage'
import { userFacingRequestError } from '@/lib/http'
import { RequestErrorToast } from '@/components/request-error-toast'
import type { AuthProvider } from '@/lib/types/auth'
import { SettingsTimezoneCard } from './settings-timezone-card'

const passwordSchema = z
  .object({
    old_password: z.string().min(1, 'Enter your current password'),
    new_password: z.string().min(6, 'Use at least 6 characters').max(128, 'Password is too long'),
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
  display_name: z.string().trim().min(1, 'Enter your name').max(100, 'Use 100 characters or fewer'),
})

type PasswordValues = z.infer<typeof passwordSchema>
type ProfileValues = z.infer<typeof profileSchema>

export function SettingsPage() {
  const { t: translate } = useTranslation()
  const { user, owner, isCurrent, mustChangePassword, refreshUser, refreshReplacement, logout } = useAuth()
  const [mountedOwner] = useState(() => owner)
  const formId = useId()
  const [providers, setProviders] = useState<AuthProvider[]>([])
  const tenants = useQuery({ queryKey: ['my-tenants'], queryFn: () => fetchMyTenants(owner) })

  useEffect(() => {
    const expectedOwner = mountedOwner
    let cancelled = false
    void listAuthProviders(undefined, expectedOwner)
      .then((rows) => {
        if (!cancelled && isCurrent(expectedOwner)) setProviders(rows.filter((row) => row.is_active))
      })
      .catch((error: unknown) => {
        if (!cancelled && isCurrent(expectedOwner)) {
          setProviders([])
          toast.error(userFacingRequestError(error, translate("Unable to load sign-in providers. Try again.")))
        }
      })
    return () => { cancelled = true }
  }, [mountedOwner, isCurrent, translate])

  const passwordForm = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { old_password: '', new_password: '', confirm_password: '' },
  })
  const profileForm = useForm<ProfileValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: { display_name: user?.display_name ?? '' },
  })

  async function onPassword(values: PasswordValues) {
    const expectedOwner = mountedOwner
    if (!isCurrent(expectedOwner)) return
    try {
      const result = await changePasswordRequest({
        old_password: values.old_password,
        new_password: values.new_password,
      }, expectedOwner)
      if (!isCurrent(expectedOwner)) return
      if (!result.access_token) { logout(); return }
      const installedOwner = await refreshReplacement(expectedOwner, result.access_token)
      if (!installedOwner || !isCurrent(installedOwner)) return
      passwordForm.reset()
      toast.success(mustChangePassword ? translate("Password updated. You can use the workspace now.") : translate("Password updated"))
    } catch (error) {
      if (!isCurrent(expectedOwner)) return
      toast.error(userFacingRequestError(error, translate("Unable to update password.")))
    }
  }

  async function onProfile(values: ProfileValues) {
    const expectedOwner = mountedOwner
    if (!isCurrent(expectedOwner)) return
    try {
      await updateCurrentUser({
        display_name: values.display_name,
      }, expectedOwner)
      if (!isCurrent(expectedOwner) || !await refreshUser(expectedOwner) || !isCurrent(expectedOwner)) return
      toast.success(translate("Profile updated"))
    } catch (error) {
      if (!isCurrent(expectedOwner)) return
      toast.error(userFacingRequestError(error, translate("Unable to update profile.")))
    }
  }

  async function onSwitch(tenantId: string | null) {
    const expectedOwner = owner
    if (!tenantId || !isCurrent(expectedOwner)) return
    try {
      const result = await switchTenant(tenantId, expectedOwner)
      if (!isCurrent(expectedOwner)) return
      const installedOwner = await refreshReplacement(expectedOwner, result.access_token)
      if (!installedOwner || !isCurrent(installedOwner)) return
      toast.success(translate("Switched organization"))
    } catch {
      if (!isCurrent(expectedOwner)) return
      toast.error(translate("Unable to switch organization"))
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">{translate("Settings")}</h1>
        <p className="text-sm text-muted-foreground">{user?.email}</p>
      </div>

      {mustChangePassword ? (
        <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive">
          {translate("You must choose a new password before using the workspace.")}</div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>{translate("Profile")}</CardTitle>
          <CardDescription>{translate("How teammates see you in this organization.")}</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={profileForm.handleSubmit(onProfile)}>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-name`}>{translate("Display name")}</Label>
              <Input id={`${formId}-name`} {...profileForm.register('display_name')} />
            </div>
            <Button type="submit" disabled={profileForm.formState.isSubmitting}>
              {translate("Save profile")}</Button>
          </form>
        </CardContent>
      </Card>

      <SettingsTimezoneCard />

      <Card>
        <CardContent className="flex items-center justify-between gap-4 py-6">
          <div>
            <p className="font-medium">{translate('Language')}</p>
            <p className="text-sm text-muted-foreground">{translate('Choose the language used throughout MaraClaw.')}</p>
          </div>
          <LanguageSelector />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{translate("Password")}</CardTitle>
          <CardDescription>{translate("The new password must differ from the current one.")}</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={passwordForm.handleSubmit(onPassword)}>
            <PasswordField label={translate("Current password")} autoComplete="current-password" error={passwordForm.formState.errors.old_password?.message} {...passwordForm.register('old_password')} />
            <PasswordField label={translate("New password")} autoComplete="new-password" error={passwordForm.formState.errors.new_password?.message} {...passwordForm.register('new_password')} />
            <PasswordField label={translate("Confirm new password")} autoComplete="new-password" error={passwordForm.formState.errors.confirm_password?.message} {...passwordForm.register('confirm_password')} />
            <Button type="submit" disabled={passwordForm.formState.isSubmitting}>
              {passwordForm.formState.isSubmitting ? <Loader2 className="size-4 animate-spin" /> : translate("Update password")}
            </Button>
          </form>
        </CardContent>
      </Card>

      {providers.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>{translate("Connected sign-in")}</CardTitle>
            <CardDescription>{translate("Link a company SSO provider to this account, or remove it.")}</CardDescription>
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
                      const expectedOwner = mountedOwner
                      if (!isCurrent(expectedOwner)) return
                      const redirectUri = `${window.location.origin}/sso/callback`
                      void authorizeProvider(provider.provider_type, redirectUri, 'bind', expectedOwner).then((result) => {
                        if (!isCurrent(expectedOwner)) return
                        storeSsoTransaction({ state: result.state, provider: provider.provider_type, intent: 'bind' }, null)
                        window.location.assign(result.authorization_url)
                      }).catch(() => {
                        if (isCurrent(expectedOwner)) toast.error(translate("Unable to link this provider."))
                      })
                    }}
                  >
                    {translate("Connect")}</Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      const expectedOwner = mountedOwner
                      if (!isCurrent(expectedOwner)) return
                      void unbindProvider(provider.provider_type, expectedOwner).then(() => {
                        if (isCurrent(expectedOwner)) toast.success(translate("Unlinked"))
                      }).catch(() => {
                        if (isCurrent(expectedOwner)) toast.error(translate("Unable to unlink this provider."))
                      })
                    }}
                  >
                    {translate("Remove")}</Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>{translate("Organization")}</CardTitle>
          <CardDescription>{translate("Legacy multi-membership only. Most members belong to one company.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {tenants.isError ? <RequestErrorToast error={tenants.error} message={userFacingRequestError(tenants.error, translate("Unable to load organizations. Try again."))} /> : null}
          {(tenants.data ?? []).map((tenant) => (
            <div key={tenant.tenant_id ?? tenant.tenant_slug} className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{tenant.tenant_name}</p>
                <p className="text-xs text-muted-foreground">{tenant.tenant_slug}</p>
              </div>
              <Button size="sm" onClick={() => void onSwitch(tenant.tenant_id)}>
                {translate("Switch")}</Button>
            </div>
          ))}
          <Button asChild>
            <Link to="/transfer">{translate("Transfer organization")}</Link>
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{translate("Session")}</CardTitle>
          <CardDescription>
            {translate("Signed in as {member}. Sign out ends this browser session.", { member: user?.display_name || user?.email || translate("this member") })}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button type="button" onClick={logout}>
            <LogOut className="size-4" aria-hidden />
            {translate("Sign out")}</Button>
        </CardContent>
      </Card>
    </div>
  )
}
