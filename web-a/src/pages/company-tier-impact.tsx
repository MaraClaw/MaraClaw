import { useTranslation } from '@/hooks/use-translation'
import type { CompanyTierPreview } from '@/lib/companies-api'
import { tierName } from '@/lib/resource-pressure-format'

export function CompanyTierImpact({ preview }: { readonly preview: CompanyTierPreview }) {
  const { t, number, date } = useTranslation()
  return (
    <div className="flex flex-col gap-4 text-sm">
      <p className="font-medium">{t('Tier change: {current} to {target}', { current: tierName(preview.current_tier), target: tierName(preview.target_tier) })}</p>
      <dl className="grid gap-4 sm:grid-cols-2">
        {(['accounts', 'agents'] as const).map(kind => {
          const capacity = preview[kind]
          return <div key={kind}>
            <dt className="font-medium">{t(kind === 'accounts' ? 'Accounts' : 'Agents')}</dt>
            <dd>{t('{used} used; {included} included + {extras} extra = {limit} limit.', { used: number(capacity.used), included: number(capacity.included), extras: number(capacity.extras), limit: number(capacity.limit) })}</dd>
            <dd>{t('{included} over included; {limit} over total limit.', { included: number(capacity.over_included), limit: number(capacity.over_limit) })}</dd>
          </div>
        })}
      </dl>
      <p>{t('Agents per account: {limit}', { limit: preview.per_account_agent_limit === null ? t('Unlimited') : number(preview.per_account_agent_limit) })}</p>
      {preview.owners_over_limit.length ? <ul className="space-y-2">{preview.owners_over_limit.map(owner => <li key={owner.user_id}>
        {owner.display_name ?? owner.user_id}: {t('{count} agents; limit {limit}; {overage} over.', { count: number(owner.agents_count), limit: number(owner.limit), overage: number(owner.overage) })}
      </li>)}</ul> : <p className="text-muted-foreground">{t('No account exceeds the target per-account agent limit.')}</p>}
      <div>
        <p>{t('Running guests keep their current limits until they stop and resume or are replaced.')}</p>
        {preview.running_guests.length ? <ul className="mt-2 space-y-2">{preview.running_guests.map(guest => <li key={guest.agent_id}>{guest.name} · {guest.container_id}</li>)}</ul> : <p className="mt-2 text-muted-foreground">{t('No running guests.')}</p>}
      </div>
      <div>
        <p className="font-medium">{t('Next capacity renewal')}</p>
        <p>{date(preview.next_capacity_renewal.billing_month, { dateStyle: 'medium', timeZone: 'UTC' })}: {t('{accounts} extra accounts at {accountPrice} USD; {agents} extra agents at {agentPrice} USD; total {total} USD per month.', { accounts: number(preview.next_capacity_renewal.extra_accounts), accountPrice: number(preview.next_capacity_renewal.account_unit_usd), agents: number(preview.next_capacity_renewal.extra_agents), agentPrice: number(preview.next_capacity_renewal.agent_unit_usd), total: number(preview.next_capacity_renewal.monthly_usd) })}</p>
        <p className="mt-2 text-muted-foreground">{t('Existing orders keep their recorded tier. Running containers will not be changed.')}</p>
      </div>
    </div>
  )
}
