import { Link } from 'react-router-dom'

import { capacityActionClass, capacityKindLabels } from '@/components/capacity/capacity-labels'
import { CapacityStatusBadge } from '@/components/capacity/capacity-status-badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { useCapacityAccess, useCapacityOrders, useCapacityOverview } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import { CAPACITY_RECENT_ORDERS_LIMIT, type CapacityOrder, type CapacityOverview } from '@/lib/capacity-api'
import { formatBillingMonth, formatCalendarDate, formatVnd } from '@/lib/capacity-format'
import { isRenewalLapsed } from '@/lib/capacity-order-state'
import { ApiError } from '@/lib/http'
import { CapacityBuyForm } from './users-capacity-form'

function UsageTile({ label, used, limit, included, extra, full }: {
  readonly label: string; readonly used: number; readonly limit: number
  readonly included: number; readonly extra: number; readonly full: string
}) {
  const { t, number } = useTranslation()
  return <div className="rounded-xl border border-border bg-card p-4">
    <dt className="text-sm text-muted-foreground">{label}</dt>
    <dd className="mt-1 font-display text-2xl font-semibold tabular-nums">{t('{used} of {limit}', { used: number(used), limit: number(limit) })}</dd>
    <dd className="mt-1 text-xs tabular-nums text-muted-foreground">{t('{included} included + {extra} extra', { included: number(included), extra: number(extra) })}</dd>
    {used >= limit ? <dd className="mt-2 text-sm leading-relaxed">{full}</dd> : null}
  </div>
}

function RenewalNote({ order, today }: { readonly order: CapacityOrder; readonly today: string }) {
  const { t, locale } = useTranslation()
  const { canBuy } = useCapacityAccess()
  const month = formatBillingMonth(order.billing_month, locale)
  const amount = formatVnd(order.amount_vnd, locale)
  const deadline = order.grace_until === null ? '' : formatCalendarDate(order.grace_until, locale)
  const text = order.status === 'awaiting_payment'
    ? isRenewalLapsed(order, today)
      ? t('{month} renewal was due by {date}. Your extra accounts and agents no longer count. Pay {amount} to restore them.', { month, date: deadline, amount })
      : t('{month} renewal: {amount}, pay by {date}', { month, amount, date: deadline })
    : order.status === 'submitted' ? t('{month} renewal: payment sent, waiting for review.', { month })
      : order.status === 'approved' ? t('{month} renewal: paid.', { month })
        : order.status === 'rejected' ? t('{month} renewal: payment rejected. Open the order to read the reviewer’s note.', { month })
          : t('{month} renewal: not paid.', { month })
  return <Card>
    <CardContent className="flex flex-wrap items-center justify-between gap-3">
      <p className="min-w-0 text-sm leading-relaxed [overflow-wrap:anywhere]">{text}</p>
      <Button asChild variant="outline" className={capacityActionClass}>
        <Link to={`/capacity-orders/${order.id}`}>{t(order.status === 'awaiting_payment' && canBuy ? 'Open payment page' : 'View order')}</Link>
      </Button>
    </CardContent>
  </Card>
}

function OrderRow({ order }: { readonly order: CapacityOrder }) {
  const { t, locale, date } = useTranslation()
  return <li className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-xl border border-border bg-card p-4">
    <div className="flex min-w-0 flex-col gap-1">
      <p className="flex flex-wrap items-center gap-2 text-sm"><CapacityStatusBadge status={order.status} /><span className="font-medium tabular-nums">{order.reference}</span></p>
      <p className="text-xs text-muted-foreground tabular-nums">{t(capacityKindLabels[order.kind])} · {formatVnd(order.amount_vnd, locale)} · <time dateTime={order.created_at}>{date(order.created_at)}</time></p>
    </div>
    <Button asChild variant="outline" className={capacityActionClass}>
      <Link to={`/capacity-orders/${order.id}`} aria-label={t('View order {reference}', { reference: order.reference })}>{t('View order')}</Link>
    </Button>
  </li>
}

function CappedCapacity({ overview, tenantId }: { readonly overview: CapacityOverview; readonly tenantId: string }) {
  const { t } = useTranslation()
  const { canBuy } = useCapacityAccess()
  const orders = useCapacityOrders(tenantId, CAPACITY_RECENT_ORDERS_LIMIT)
  const openPurchase = overview.open_orders.find((order) => order.kind === 'purchase' && order.status === 'awaiting_payment') ?? null
  const items = orders.data?.items ?? []
  const { account_limit: accountLimit, agent_limit: agentLimit, included_accounts: includedAccounts, included_agents: includedAgents } = overview

  return <>
    {accountLimit !== null && agentLimit !== null && includedAccounts !== null && includedAgents !== null ? <dl className="grid gap-4 sm:grid-cols-2">
      <UsageTile label={t('Accounts')} used={overview.accounts_used} limit={accountLimit} included={includedAccounts} extra={overview.extra_accounts} full={t('Limit reached. New accounts cannot join until you add more.')} />
      <UsageTile label={t('Agents')} used={overview.agents_used} limit={agentLimit} included={includedAgents} extra={overview.extra_agents} full={t('Limit reached. New agents cannot be created until you add more.')} />
    </dl> : null}
    {overview.current_renewal !== null ? <RenewalNote order={overview.current_renewal} today={overview.today} /> : null}
    {overview.open_orders.filter((order) => order.id !== overview.current_renewal?.id).map((order) => <RenewalOpenOrder key={order.id} order={order} />)}
    {canBuy ? <CapacityBuyForm overview={overview} openPurchase={openPurchase} /> : <p className="text-sm leading-relaxed text-muted-foreground">{t('You are viewing this company as a platform admin. Only its organization admins can place orders.')}</p>}
    <Card>
      <CardHeader><h3 className="font-display text-base font-semibold">{t('Recent orders')}</h3></CardHeader>
      <CardContent className="flex flex-col gap-3">
        {orders.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('Loading orders...')}</p> : null}
        {orders.isError ? <div className="flex flex-col items-start gap-3">
          <p role="alert" className="text-sm text-destructive">{orders.error instanceof ApiError ? orders.error.message : t('Could not load orders. Check your connection and retry.')}</p>
          <Button variant="outline" className={capacityActionClass} disabled={orders.isFetching} onClick={() => { void orders.refetch() }}>{t('Retry')}</Button>
        </div> : null}
        {orders.isSuccess && items.length === 0 ? <p className="text-sm text-muted-foreground">{t('No orders yet.')}</p> : null}
        {items.length > 0 ? <ul aria-label={t('Recent orders')} className="flex flex-col gap-3">{items.map((order) => <OrderRow key={order.id} order={order} />)}</ul> : null}
      </CardContent>
    </Card>
  </>
}

function RenewalOpenOrder({ order }: { readonly order: CapacityOrder }) {
  const { t, locale } = useTranslation()
  return <Card>
    <CardContent className="flex flex-wrap items-center justify-between gap-3">
      <p className="min-w-0 text-sm leading-relaxed [overflow-wrap:anywhere]">{t('{kind} {reference}: {amount}', { kind: t(capacityKindLabels[order.kind]), reference: order.reference, amount: formatVnd(order.amount_vnd, locale) })} <CapacityStatusBadge status={order.status} /></p>
      <Button asChild variant="outline" className={capacityActionClass}>
        <Link to={`/capacity-orders/${order.id}`}>{t(order.status === 'awaiting_payment' ? 'Open payment page' : 'View order')}</Link>
      </Button>
    </CardContent>
  </Card>
}

export function UsersCapacity({ tenantId }: { readonly tenantId: string }) {
  const { t } = useTranslation()
  const overview = useCapacityOverview(tenantId)

  return <section aria-labelledby="users-capacity-heading" className="flex flex-col gap-4">
    <div>
      <h2 id="users-capacity-heading" className="font-display text-lg font-semibold tracking-tight">{t('Extra accounts and agents')}</h2>
      <p className="mt-1 text-sm text-muted-foreground">{t('Your plan includes a set number of accounts and agents. Buy more when you reach the limit.')}</p>
    </div>
    {overview.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('Loading account and agent limits...')}</p> : null}
    {overview.isError ? <div className="flex flex-col items-start gap-3">
      <p role="alert" className="text-sm text-destructive">{overview.error instanceof ApiError ? overview.error.message : t('Could not load account and agent limits. Check your connection and retry.')}</p>
      <Button variant="outline" className={capacityActionClass} disabled={overview.isFetching} onClick={() => { void overview.refetch() }}>{t('Retry')}</Button>
    </div> : null}
    {overview.data && !overview.data.capped ? <p className="text-sm leading-relaxed text-muted-foreground">{t('This company has no account or agent limits, so there is nothing to buy.')}</p> : null}
    {overview.data?.capped ? <CappedCapacity key={tenantId} overview={overview.data} tenantId={tenantId} /> : null}
  </section>
}
