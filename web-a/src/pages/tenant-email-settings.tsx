import { useQuery } from '@tanstack/react-query'
import { useId } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { getTenant } from '@/lib/companies-api'
import { tenantEmailScope } from '@/lib/tenant-email-access'
import { fetchTenantEmail } from '@/lib/tenant-email-api'
import { TenantEmailEditor } from './tenant-email-editor'
import { tenantEmailSource } from './tenant-email-form'

export function TenantEmailSettings() {
  const { user, mustChangePassword } = useAuth()
  const title = useId()
  const tenantId = tenantEmailScope(user)
  if (!tenantId) return null
  return <Card role="region" aria-labelledby={title}>
    <CardHeader>
      <h2 id={title} className="text-lg font-semibold leading-none">Organization SMTP</h2>
      <CardDescription>Configure email delivery only for your organization. Only its genesis administrator can manage these settings.</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5 [overflow-wrap:anywhere]">
      {mustChangePassword ? <p className="text-sm text-muted-foreground">Change your password on <Link to="/account" className="text-primary underline underline-offset-4">Account</Link> before viewing or changing SMTP settings.</p> : <ScopedEmailSettings key={tenantId} tenantId={tenantId} recipient={user?.email ?? null} />}
    </CardContent>
  </Card>
}

function ScopedEmailSettings({ tenantId, recipient }: { readonly tenantId: string; readonly recipient: string | null }) {
  const own = useQuery({ queryKey: ['admin-smtp-own-organization', tenantId], queryFn: ({ signal }) => getTenant(tenantId, signal), retry: false })
  const config = useQuery({ queryKey: ['admin-tenant-email', tenantId], queryFn: ({ signal }) => fetchTenantEmail(tenantId, signal), retry: false })
  return <>
    <p className="text-sm text-muted-foreground">Your organization: {own.data?.name ?? (own.isError ? 'name unavailable' : 'loading…')}. These settings apply only to your organization.</p>
    {config.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading SMTP settings…</p> : null}
    {config.isError ? <div className="space-y-3"><p role="alert" className="text-sm text-destructive">Could not load SMTP settings. Check the connection and try again.</p><Button type="button" variant="outline" onClick={() => { void config.refetch() }}>Retry loading SMTP settings</Button></div> : null}
    {config.data ? <>
      <p className="text-sm text-muted-foreground">{tenantEmailSource(config.data.effective_source)}</p>
      <TenantEmailEditor tenantId={tenantId} saved={config.data} recipient={recipient} />
    </> : null}
  </>
}
