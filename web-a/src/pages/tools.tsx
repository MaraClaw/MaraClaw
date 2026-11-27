import { useSearchParams } from 'react-router-dom'

import { CompanyPicker, useCompanyPicker } from '@/components/companies/company-picker'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { isPlatformAdminUser } from '@/lib/types/auth-guards'
import { ToolsCatalog } from './tools-catalog'
import { toolButtonClass } from './tools-fields'
import { toolsTenantId } from './tools-forms'

export function ToolsPage() {
  const { t } = useTranslation()
  const { user, owner } = useAuth()
  const [params, setParams] = useSearchParams()
  const platform = isPlatformAdminUser(user)
  const tenantId = toolsTenantId(user, params.get('company'))
  const companies = useCompanyPicker({ value: platform ? tenantId ?? '' : '', enabled: platform })
  const concreteCompany = !!tenantId && (!platform || companies.status === 'resolved')
  return <div className="mx-auto flex w-full min-w-0 max-w-5xl flex-col gap-6">
    <header className="space-y-2">
      <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Tools')}</h1>
      <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">{t('Manage the live tool catalog, MCP connections and configuration. Built-in and global records are shared across companies. Company-owned records affect only their owning company and agents assigned to the tool.')}</p>
    </header>
    <Card><CardContent className="space-y-4">
      {platform ? <div className="max-w-lg space-y-2">
        <CompanyPicker picker={companies} label={t('Company context')} emptyLabel={t('Current / platform scope')} unknownLabel={t('Unrecognized company selection')} describedBy="tools-company-help" onChange={(company) => {
          const next = new URLSearchParams(params)
          if (company) next.set('company', company)
          else next.delete('company')
          setParams(next)
        }} />
        {companies.error ? <div className="space-y-2"><ErrorToast error={companies.error} message={t('Could not load companies.')} /><Button variant="outline" className={toolButtonClass} onClick={companies.retry}>{t('Retry loading companies')}</Button></div> : null}
      </div> : <p className="text-sm font-medium">{t('Company context: your organization')}</p>}
      <p id="tools-company-help" className="text-sm leading-relaxed text-muted-foreground">{concreteCompany
        ? t('This selection scopes catalog visibility, new MCP tools and built-in source configuration. Each record shows its ownership; company selection does not change who owns it. Available actions follow the permissions returned by the server for this context.')
        : t('Inspect record ownership and server-permitted actions in this catalog. Select a concrete company before creating MCP tools or configuring a tool. Current / platform scope uses the server’s current context, which may include multiple companies.')}</p>
    </CardContent></Card>
    <ToolsCatalog key={`${owner.generation}:${tenantId ?? 'current'}:${concreteCompany}`} tenantId={tenantId} concreteCompany={concreteCompany} />
  </div>
}
