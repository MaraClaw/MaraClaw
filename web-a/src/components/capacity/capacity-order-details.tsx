import type { ReactNode } from 'react'

import { useTranslation } from '@/hooks/use-translation'
import type { CapacityOrder } from '@/lib/capacity-api'
import { formatBillingMonth, formatCalendarDate, formatFileSize, formatUsd, formatUsdCents, formatVnd } from '@/lib/capacity-format'
import { isProrated } from '@/lib/capacity-order-state'
import { capacityKindLabels, capacityTierLabels } from './capacity-labels'

function Field({ label, children }: { readonly label: string; readonly children: ReactNode }) {
  return <div className="min-w-0">
    <dt className="text-xs text-muted-foreground">{label}</dt>
    <dd className="mt-1 text-sm leading-relaxed">{children}</dd>
  </div>
}

export function CapacityOrderDetails({ order, showCompany = false }: { readonly order: CapacityOrder; readonly showCompany?: boolean }) {
  const { t, locale, number, date } = useTranslation()
  const month = formatBillingMonth(order.billing_month, locale)
  const monthlyUsd = order.extra_accounts * order.account_unit_usd + order.extra_agents * order.agent_unit_usd
  const period = isProrated(order)
    ? t('Prorated: {remaining} of {days} days left in {month}', { remaining: number(order.days_charged), days: number(order.days_in_month), month })
    : t('Full month: {month}', { month })

  return <dl className="grid gap-4 sm:grid-cols-2">
    {showCompany && order.tenant_name ? <Field label={t('Company')}>{order.tenant_name}</Field> : null}
    <Field label={t('Reference')}><span className="font-medium tabular-nums">{order.reference}</span></Field>
    <Field label={t('Type')}>{t(capacityKindLabels[order.kind])}</Field>
    <Field label={t('Plan')}>{capacityTierLabels[order.tier]}</Field>
    <Field label={t('Billing period')}>{period}</Field>
    <Field label={t('Extra accounts')}>
      <span className="tabular-nums">{t('{count} × {price} a month', { count: number(order.extra_accounts), price: formatUsd(order.account_unit_usd, locale) })}</span>
    </Field>
    <Field label={t('Extra agents')}>
      <span className="tabular-nums">{t('{count} × {price} a month', { count: number(order.extra_agents), price: formatUsd(order.agent_unit_usd, locale) })}</span>
    </Field>
    <Field label={t('Price for a full month')}><span className="tabular-nums">{formatUsd(monthlyUsd, locale)}</span></Field>
    <Field label={t('Amount in USD')}><span className="tabular-nums">{formatUsdCents(order.amount_usd_cents, locale)}</span></Field>
    <Field label={t('Exchange rate')}><span className="tabular-nums">{t('{rate} VND per USD', { rate: number(order.usd_vnd_rate) })}</span></Field>
    <Field label={t('Amount in VND')}><span className="tabular-nums">{formatVnd(order.amount_vnd, locale)}</span></Field>
    {order.grace_until !== null ? <Field label={t('Pay by')}>{formatCalendarDate(order.grace_until, locale)}</Field> : null}
    <Field label={t('Created')}>{date(order.created_at)}</Field>
    {order.submitted_at !== null ? <Field label={t('Sent for review')}>{date(order.submitted_at)}</Field> : null}
    {order.reviewed_at !== null ? <Field label={t('Reviewed')}>{date(order.reviewed_at)}</Field> : null}
    {order.evidence !== null ? <Field label={t('Payment evidence')}>
      <span className="[overflow-wrap:anywhere]">{order.evidence.filename}</span> <span className="tabular-nums text-muted-foreground">({formatFileSize(order.evidence.size_bytes, locale)})</span>
    </Field> : null}
  </dl>
}
