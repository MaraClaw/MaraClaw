import { useQuery } from '@tanstack/react-query'
import { LogOut } from 'lucide-react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { fetchMyTenants, switchTenant } from '@/lib/auth-api'

export function SettingsPage() {
  const { user, owner, isCurrent, refreshReplacement, logout } = useAuth()
  const tenants = useQuery({ queryKey: ['my-tenants'], queryFn: () => fetchMyTenants(owner) })

  async function onSwitch(tenantId: string | null) {
    const expectedOwner = owner
    if (!tenantId || !isCurrent(expectedOwner)) return
    try {
      const result = await switchTenant(tenantId, expectedOwner)
      if (!isCurrent(expectedOwner)) return
      const installedOwner = await refreshReplacement(expectedOwner, result.access_token)
      if (!installedOwner || !isCurrent(installedOwner)) return
      toast.success('Switched organization')
    } catch {
      if (!isCurrent(expectedOwner)) return
      toast.error('Unable to switch organization')
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">Organization and session.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Organization</CardTitle>
          <CardDescription>Legacy multi-membership only. Most members belong to one company.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {(tenants.data ?? []).map((tenant) => (
            <div key={tenant.tenant_id ?? tenant.tenant_slug} className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{tenant.tenant_name}</p>
                <p className="text-xs text-muted-foreground">{tenant.tenant_slug}</p>
              </div>
              <Button size="sm" variant="outline" onClick={() => void onSwitch(tenant.tenant_id)}>
                Switch
              </Button>
            </div>
          ))}
          <Button asChild variant="outline">
            <Link to="/transfer">Transfer organization</Link>
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Session</CardTitle>
          <CardDescription>
            Signed in as {user?.display_name || user?.email || 'this member'}. Sign out ends this
            browser session.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button type="button" variant="outline" onClick={logout}>
            <LogOut className="size-4" aria-hidden />
            Sign out
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
