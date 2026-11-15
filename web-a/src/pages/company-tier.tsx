import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { companyTierChangeOptions, companyTierPreviewOptions } from '@/hooks/company-options'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import type { CompanyStats } from '@/lib/companies-api'
import { OrganizationPlanIdSchema, OrganizationPlanIdValues } from '@/lib/engine-contracts.generated'
import { ApiError } from '@/lib/http'
import { tierName } from '@/lib/resource-pressure-format'
import { CompanyTierImpact } from './company-tier-impact'

export function CompanyTier({ company }: { readonly company: CompanyStats }) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const id = useId()
  const [target, setTarget] = useState(company.tier)
  const [reviewedTarget, setReviewedTarget] = useState<typeof target | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const preview = useQuery(companyTierPreviewOptions(company.id, reviewedTarget))
  const options = companyTierChangeOptions(client, company.id)
  const change = useSessionMutation({
    ...options,
    onError: (error) => { setConfirmed(false); options.onError(error) },
    onSuccess: () => { setConfirmed(false); setReviewedTarget(null); options.onSuccess() },
  })
  const ready = preview.isSuccess && !preview.isFetching && preview.data.target_tier === target && preview.data.current_tier === company.tier
  const busy = preview.isFetching || change.isPending

  return (
    <Card className="min-w-0 [overflow-wrap:anywhere]">
      <CardHeader><CardTitle>{t('Company tier')}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-4">
        {company.is_system ? <p className="text-sm text-muted-foreground">{t('System company tiers are managed by the platform and cannot be changed here.')}</p> : <>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-full space-y-2 sm:max-w-xs">
              <Label htmlFor={id}>{t('Target tier')}</Label>
              <Select id={id} value={target} disabled={change.isPending} onChange={(event) => {
                setTarget(OrganizationPlanIdSchema.parse(event.target.value))
                setReviewedTarget(null)
                setConfirmed(false)
                change.reset()
              }}>
                {OrganizationPlanIdValues.map(tier => <option key={tier} value={tier}>{tierName(tier)}</option>)}
              </Select>
            </div>
            <Button type="button" variant="outline" disabled={target === company.tier || busy} onClick={() => {
              setConfirmed(false)
              setReviewedTarget(target)
              if (reviewedTarget === target) void preview.refetch()
            }}>{t('Preview tier change')}</Button>
          </div>
          <p role="status" className="text-sm text-muted-foreground empty:hidden">{preview.isFetching ? t('Loading tier preview...') : null}{change.isPending ? t('Changing tier...') : null}</p>
          {preview.error ? <p role="alert" className="text-sm text-destructive">{preview.error instanceof ApiError ? preview.error.message : t('Could not load tier preview. Retry the preview.')}</p> : null}
          {ready ? <>
            <CompanyTierImpact preview={preview.data} />
            <Label className="flex min-h-11 items-center gap-3" htmlFor={`${id}-confirm`}>
              <input id={`${id}-confirm`} type="checkbox" className="size-5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-ring" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />
              {t('I reviewed the tier impact for this company.')}
            </Label>
            <Button type="button" className="w-fit" disabled={!confirmed || busy} onClick={() => {
              if (!ready || !confirmed || busy) return
              change.mutate({ target_tier: preview.data.target_tier, expected_tier: preview.data.current_tier })
            }}>{t('Confirm tier change')}</Button>
          </> : null}
          {change.error ? <p role="alert" className="text-sm text-destructive">{change.error instanceof ApiError && change.error.status === 409
            ? t('The company tier changed. Review the refreshed preview before confirming again.')
            : change.error instanceof ApiError ? change.error.message : t('Could not change company tier.')}</p> : null}
        </>}
      </CardContent>
    </Card>
  )
}
