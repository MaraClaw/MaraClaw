import { useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { capacityActionClass, capacityKindLabels, capacityStatusLabels } from '@/components/capacity/capacity-labels'
import { CapacityStatusBadge } from '@/components/capacity/capacity-status-badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { capacityKeys } from '@/hooks/capacity-options'
import { useCapacityAccess, useCapacityReviewOrders } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import { CAPACITY_ORDERS_PAGE_SIZE, capacityOrderStatusSchema, type CapacityOrderStatus } from '@/lib/capacity-api'
import { formatVnd } from '@/lib/capacity-format'
import { ApiError } from '@/lib/http'
import { CapacityPaymentSettingsSection } from './capacity-payment-settings'

export function CapacityPaymentsPage() {
  const { t, locale, date, distance, number } = useTranslation()
  const { canManageSettings } = useCapacityAccess()
  const filterId = useId()
  const client = useQueryClient()
  const [status, setStatus] = useState<CapacityOrderStatus>('submitted')
  const [offset, setOffset] = useState(0)
  const reviews = useCapacityReviewOrders(status, offset)
  const items = reviews.data?.items ?? []

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header>
      <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Capacity payments')}</h1>
      <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t('Check bank transfers for extra accounts and agents. Approving a payment raises that company’s limits.')}</p>
    </header>
    {canManageSettings ? <CapacityPaymentSettingsSection /> : null}
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor={filterId}>{t('Payment status')}</Label>
        <Select id={filterId} fit value={status} onChange={(event) => {
          setStatus(capacityOrderStatusSchema.parse(event.currentTarget.value))
          setOffset(0)
        }}>
          {capacityOrderStatusSchema.options.map((value) => <option key={value} value={value}>{t(capacityStatusLabels[value])}</option>)}
        </Select>
      </div>
      <Button variant="outline" className={capacityActionClass} disabled={reviews.isFetching} onClick={() => { setOffset(0); void client.invalidateQueries({ queryKey: capacityKeys.all }) }}>{t('Refresh payments')}</Button>
    </div>
    {reviews.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">{t('Loading payments...')}</CardContent></Card> : null}
    {reviews.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
      <ErrorToast error={reviews.error} message={reviews.error instanceof ApiError ? reviews.error.message : t('Could not load payments. Check your connection and retry.')} />
      <Button variant="outline" className={capacityActionClass} disabled={reviews.isFetching} onClick={() => { void reviews.refetch() }}>{t('Retry')}</Button>
    </CardContent></Card> : null}
    {reviews.isSuccess && items.length === 0 ? <Card><CardContent className="flex flex-col gap-2 py-4">
      <h2 className="font-display text-lg font-semibold">{t('No payments with this status')}</h2>
      <p className="text-sm text-muted-foreground">{t(offset > 0 ? 'Return to the previous page to see earlier payments.' : 'Refresh to check for new payments, or choose another status.')}</p>
    </CardContent></Card> : null}
    {items.length > 0 ? <ul aria-label={t('Capacity payments')} className="flex flex-col gap-4">{items.map((order) => <li key={order.id}>
      <Card><CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CapacityStatusBadge status={order.status} />
          <time dateTime={order.submitted_at ?? order.created_at} title={date(order.submitted_at ?? order.created_at)} className="text-xs text-muted-foreground">{distance(order.submitted_at ?? order.created_at)}</time>
        </div>
        <h2 className="font-display text-base font-semibold">{order.tenant_name ?? t('Unknown company')}</h2>
        <p className="text-sm tabular-nums">{order.reference} · {t(capacityKindLabels[order.kind])}</p>
        <p className="text-sm tabular-nums text-muted-foreground">{t('Extra accounts: {count}', { count: number(order.extra_accounts) })} · {t('Extra agents: {count}', { count: number(order.extra_agents) })}</p>
        <p className="text-sm font-medium tabular-nums">{formatVnd(order.amount_vnd, locale)}</p>
        <Button asChild variant="outline" className={`self-start ${capacityActionClass}`}><Link to={`/capacity-payments/${order.id}`} aria-label={t('Review payment {reference}', { reference: order.reference })}>{t('Review payment')}</Link></Button>
      </CardContent></Card>
    </li>)}</ul> : null}
    {offset > 0 || items.length > 0 ? <nav aria-label={t('Capacity payment pages')} className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="outline" className={capacityActionClass} disabled={offset === 0 || reviews.isFetching} onClick={() => setOffset((value) => Math.max(0, value - CAPACITY_ORDERS_PAGE_SIZE))}>{t('Previous')}</Button>
      <p role="status" className="text-xs tabular-nums text-muted-foreground">{t('Page {page}', { page: number(offset / CAPACITY_ORDERS_PAGE_SIZE + 1) })}</p>
      <Button variant="outline" className={capacityActionClass} disabled={!reviews.data?.has_more || !reviews.isSuccess || reviews.isFetching} onClick={() => setOffset((value) => value + CAPACITY_ORDERS_PAGE_SIZE)}>{t('Next')}</Button>
    </nav> : null}
  </div>
}
