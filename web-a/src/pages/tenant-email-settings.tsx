import { useQuery } from '@tanstack/react-query'
import { useId } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { getTenant } from '@/lib/companies-api'
import { tenantEmailScope } from '@/lib/tenant-email-access'
import { fetchTenantEmail } from '@/lib/tenant-email-api'
import { TenantEmailEditor } from './tenant-email-editor'
import { tenantEmailSource } from './tenant-email-form'
import { useTranslation } from '@/hooks/use-translation'

export function TenantEmailSettings() {
  const { t } = useTranslation()
  const { user, mustChangePassword } = useAuth()
  const title = useId()
  const tenantId = tenantEmailScope(user)
  if (!tenantId) return null
  return <Card role="region" aria-labelledby={title}>
    <CardHeader>
      <h2 id={title} className="text-lg font-semibold leading-none">{t('Organization SMTP')}</h2>
      <CardDescription>{t('Configure email delivery only for your organization. Only its genesis administrator can manage these settings.')}</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5 [overflow-wrap:anywhere]">
      {mustChangePassword ? <p className="text-sm text-muted-foreground"><a href="#change-password" className="text-primary underline underline-offset-4">{t('Change your password')}</a> {t('before viewing or changing SMTP settings.')}</p> : <ScopedEmailSettings key={tenantId} tenantId={tenantId} recipient={user?.email ?? null} />}
    </CardContent>
  </Card>
}

function ScopedEmailSettings({ tenantId, recipient }: { readonly tenantId: string; readonly recipient: string | null }) {
  const { t, owned } = useTranslation()
  const own = useQuery({ queryKey: ['admin-smtp-own-organization', tenantId], queryFn: ({ signal }) => getTenant(tenantId, signal), retry: false })
  const config = useQuery({ queryKey: ['admin-tenant-email', tenantId], queryFn: ({ signal }) => fetchTenantEmail(tenantId, signal), retry: false })
  return <>
    <p className="text-sm text-muted-foreground">{t('Your organization: {name}. These settings apply only to your organization.', { name: own.data?.name ?? t(own.isError ? 'name unavailable' : 'loading…') })}</p>
    {config.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('Loading SMTP settings…')}</p> : null}
            {config.isError ? <div className="space-y-3"><ErrorToast error={config.error} message={t('Could not load SMTP settings. Check the connection and try again.')} /><Button type="button" variant="outline" onClick={() => { void config.refetch() }}>{t('Retry loading SMTP settings')}</Button></div> : null}
            {own.isError ? <ErrorToast error={own.error} message={t('Could not load your organization name.')} /> : null}
    {config.data ? <>
      <p className="text-sm text-muted-foreground">{owned(tenantEmailSource(config.data.effective_source))}</p>
      <TenantEmailEditor tenantId={tenantId} saved={config.data} recipient={recipient} />
    </> : null}
  </>
}
