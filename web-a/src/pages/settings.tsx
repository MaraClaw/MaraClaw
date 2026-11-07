import { LogOut } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { TransferPolicySettings } from './transfer-policy-settings'
import { TenantEmailSettings } from './tenant-email-settings'
import { SettingsPassword } from './settings-password'

export function SettingsPage() {
  const { user, logout, mustChangePassword } = useAuth()
  const navigate = useNavigate()

  function handleSignOut() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <div className="space-y-2">
        {mustChangePassword ? <Badge variant="warning">Password change required</Badge> : null}
        <h1 className="font-display text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Manage your profile, organization settings, and admin credentials.
        </p>
      </div>

      {mustChangePassword ? (
        <div
          role="status"
          className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-950 dark:text-amber-100"
        >
          You signed in with a temporary password. <a href="#change-password" className="underline underline-offset-4">Choose a new password</a> before using the rest of
          the admin console.
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Profile</CardTitle>
          <CardDescription>Signed-in operator session.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm [overflow-wrap:anywhere]">
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

      <TenantEmailSettings />
      {isPlatformAdminUser(user) ? <TransferPolicySettings /> : null}
      <SettingsPassword />

      <Card>
        <CardHeader>
          <CardTitle>Session</CardTitle>
          <CardDescription>
            Signed in as {user?.display_name || user?.email || 'this operator'}. Sign out ends this
            browser session.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button type="button" variant="outline" onClick={handleSignOut}>
            <LogOut className="size-4" aria-hidden />
            Sign out
          </Button>
        </CardContent>
      </Card>

    </div>
  )
}
