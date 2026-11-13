import { LogOut } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

import { LanguageToggle } from '@/components/language-toggle'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { TransferPolicySettings } from './transfer-policy-settings'
import { TenantEmailSettings } from './tenant-email-settings'
import { OrganizationTimezoneSettings } from './organization-timezone-settings'
import { SettingsPassword } from './settings-password'
import { useTranslation } from '@/hooks/use-translation'
import { languageName } from '@/lib/i18n'
import { roleLabel } from '@/lib/users-api'

export function SettingsPage() {
  const { t, owned, locale } = useTranslation()
  const { user, logout, mustChangePassword } = useAuth()
  const navigate = useNavigate()

  function handleSignOut() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <div className="space-y-2">
        {mustChangePassword ? <Badge variant="warning">{t('Password change required')}</Badge> : null}
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Settings')}</h1>
        <p className="text-sm text-muted-foreground">
          {t('Manage your profile, organization settings, and admin credentials.')}
        </p>
      </div>

      {mustChangePassword ? (
        <div
          role="status"
          className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-950 dark:text-amber-100"
        >
          {t('You signed in with a temporary password.')} <a href="#change-password" className="underline underline-offset-4">{t('Choose a new password')}</a> {t('before using the rest of the admin console.')}
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>{t('Profile')}</CardTitle>
          <CardDescription>{t('Signed-in operator session.')}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm [overflow-wrap:anywhere]">
          <div className="flex flex-wrap justify-between gap-2 border-b border-border/70 pb-3">
            <span className="text-muted-foreground">{t('Name')}</span>
            <span className="font-medium">{user?.display_name || '-'}</span>
          </div>
          <div className="flex flex-wrap justify-between gap-2 border-b border-border/70 pb-3">
            <span className="text-muted-foreground">{t('Email')}</span>
            <span className="font-medium">{user?.email || '-'}</span>
          </div>
          <div className="flex flex-wrap justify-between gap-2">
            <span className="text-muted-foreground">{t('Role')}</span>
            <span className="font-medium">{user ? owned(roleLabel(user.role)) : '-'}</span>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('Preferences')}</CardTitle>
          <CardDescription>{t('Display settings for this browser.')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <span className="text-muted-foreground">{t('Language')}</span>
          <div className="flex items-center gap-3">
            <span lang={locale} className="font-medium">{languageName[locale]}</span>
            <LanguageToggle />
          </div>
        </CardContent>
      </Card>

      <TenantEmailSettings />
      <OrganizationTimezoneSettings />
      {isPlatformAdminUser(user) ? <TransferPolicySettings /> : null}
      <SettingsPassword />

      <Card>
        <CardHeader>
          <CardTitle>{t('Session')}</CardTitle>
          <CardDescription>
            {t('Signed in as {name}. Sign out ends this browser session.', { name: user?.display_name || user?.email || t('this operator') })}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button type="button" variant="outline" onClick={handleSignOut}>
            <LogOut className="size-4" aria-hidden />
            {t('Sign out')}
          </Button>
        </CardContent>
      </Card>

    </div>
  )
}
