import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'

import { BackLink } from '@/components/ui/back-button'
import { companyStatsOptions } from '@/hooks/company-options'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError } from '@/lib/http'
import { CompanyRuntime } from './company-runtime'

export function CompanyRuntimePage() {
  const { t } = useTranslation()
  const { companyId } = useParams<{ companyId: string }>()
  const companies = useQuery(companyStatsOptions(companyId ?? ''))
  const company = companies.data
  if (!companyId) return null
  return (
    <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <BackLink to={`/companies/${companyId}`} label={t('Back to company')} />
      <h1 className="font-display text-2xl font-semibold tracking-tight">{company?.name ?? t('Company')}</h1>
      {companies.isLoading ? <p role="status" className="text-sm text-muted-foreground">{t('Loading company...')}</p> : null}
      {companies.error ? <p role="alert" className="text-sm text-destructive">{companies.error instanceof ApiError ? companies.error.message : t('Failed to load company')}</p> : null}
      {!companies.isLoading && !companies.error && !company ? <p className="text-sm text-muted-foreground">{t('That company was not found.')}</p> : null}
      {company && !companies.error ? <CompanyRuntime key={company.id} companyId={company.id} /> : null}
    </div>
  )
}
