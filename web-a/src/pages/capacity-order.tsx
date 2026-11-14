import { ArrowLeft } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { CapacityEvidencePreview } from '@/components/capacity/capacity-evidence-preview'
import { capacityActionClass, capacityStatusLabels } from '@/components/capacity/capacity-labels'
import { CapacityOrderDetails } from '@/components/capacity/capacity-order-details'
import { CapacityCancelOrder, CapacityEvidenceForm } from '@/components/capacity/capacity-payment-actions'
import { CapacityStatusBadge } from '@/components/capacity/capacity-status-badge'
import { CopyField } from '@/components/capacity/copy-field'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { useCapacityOrder } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import { capacityOrderIdSchema, type CapacityOrder } from '@/lib/capacity-api'
import { formatBillingMonth, formatCalendarDate, formatUsdCents, formatVnd } from '@/lib/capacity-format'
import { canCancelOrder, canSubmitEvidence, vietnamToday } from '@/lib/capacity-order-state'
import { ApiError } from '@/lib/http'
import type { Translator } from '@/lib/i18n'
import { isPlatformAdminUser } from '@/lib/types/auth'

function PaymentInstructions({ order }: { readonly order: CapacityOrder }) {
  const { t, locale, number } = useTranslation()
  const lapsed = order.grace_until !== null && vietnamToday() > order.grace_until
  return <Card>
    <CardHeader>
      <h2 className="font-display text-lg font-semibold">{t('Transfer this amount')}</h2>
      <p className="text-sm leading-relaxed text-muted-foreground">{t(order.kind === 'renewal'
        ? 'Make a bank transfer to the account below. Your extra accounts and agents continue once a platform admin approves the payment.'
        : 'Make a bank transfer to the account below. Your extra accounts and agents start once a platform admin approves the payment.')}</p>
    </CardHeader>
    <CardContent className="flex flex-col gap-5">
      <dl className="grid gap-5 sm:grid-cols-2">
        <CopyField className="sm:col-span-2" large label={t('Amount')} value={formatVnd(order.amount_vnd, locale)} copyValue={String(order.amount_vnd)} />
        <CopyField label={t('Bank name')} value={order.recipient.bank_name} />
        <CopyField label={t('Account number')} value={order.recipient.account_number} />
        <CopyField label={t('Branch name')} value={order.recipient.branch_name} />
        <CopyField label={t('Transfer note')} value={order.reference} />
      </dl>
      <p className="text-sm leading-relaxed text-muted-foreground tabular-nums">{t('{usd} at {rate} VND per USD, the rate saved when this order was created.', { usd: formatUsdCents(order.amount_usd_cents, locale), rate: number(order.usd_vnd_rate) })}</p>
      <p className="text-sm font-medium leading-relaxed">{t('Write {reference} in the transfer note so we can match your payment.', { reference: order.reference })}</p>
      {order.grace_until !== null ? <p className="text-sm leading-relaxed">{lapsed
        ? t('Payment was due by {date}. Your extra accounts and agents stopped counting after that day. Pay now to restore them.', { date: formatCalendarDate(order.grace_until, locale) })
        : t('Pay by {date}. Your extra accounts and agents keep counting until then.', { date: formatCalendarDate(order.grace_until, locale) })}</p> : null}
    </CardContent>
  </Card>
}

function outcomeText(t: Translator, order: CapacityOrder, month: string): string {
  switch (order.status) {
    case 'submitted':
      return t('A platform admin will check your transfer. You will get a notification when the order is approved or rejected.')
    case 'approved':
      return order.kind === 'renewal'
        ? t('This renewal is paid. Your extra accounts and agents continue through {month}.', { month })
        : t('Your extra accounts and agents now count toward your limits.')
    case 'rejected':
      return t('A platform admin could not confirm this payment. Read the note below, then place a new order on the Users page.')
    case 'cancelled':
      return t('This order was cancelled and nothing was charged. Place a new order on the Users page if you still need more.')
    case 'expired':
      return order.kind === 'renewal'
        ? t('This renewal was not paid before the end of {month} and no longer counts. Place a new order on the Users page to buy these extras again.', { month })
        : t('This order was not paid before the end of {month}, so it is closed. Place a new order on the Users page; it is priced for the days left in the month.', { month })
    case 'awaiting_payment':
      return ''
  }
}

function OutcomePanel({ order, focusOnMount }: { readonly order: CapacityOrder; readonly focusOnMount: boolean }) {
  const { t, locale } = useTranslation()
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (focusOnMount) heading.current?.focus() }, [focusOnMount])
  return <Card>
    <CardHeader>
      <h2 ref={heading} tabIndex={-1} className="font-display text-lg font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">{t(capacityStatusLabels[order.status])}</h2>
      <p role="status" className="text-sm leading-relaxed">{outcomeText(t, order, formatBillingMonth(order.billing_month, locale))}</p>
    </CardHeader>
    {order.review_note ? <CardContent>
      <h3 className="text-sm font-medium">{t('Reviewer note')}</h3>
      <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{order.review_note}</p>
    </CardContent> : null}
  </Card>
}

function CapacityOrderView({ order, readOnly }: { readonly order: CapacityOrder; readonly readOnly: boolean }) {
  const { t } = useTranslation()
  const [startedAwaiting] = useState(() => canSubmitEvidence(order))
  return <>
    <div className="flex flex-wrap items-center gap-3"><CapacityStatusBadge status={order.status} /></div>
    {canSubmitEvidence(order) ? <>
      <PaymentInstructions order={order} />
      {readOnly
        ? <p className="text-sm leading-relaxed text-muted-foreground">{t('You are viewing this order as a platform admin. Only an organization admin can send the payment evidence.')}</p>
        : <>
          <CapacityEvidenceForm order={order} />
          {canCancelOrder(order) ? <CapacityCancelOrder order={order} /> : null}
        </>}
    </> : <OutcomePanel order={order} focusOnMount={startedAwaiting} />}
    {order.evidence !== null && !canSubmitEvidence(order) ? <Card>
      <CardHeader><h2 className="font-display text-lg font-semibold">{t('Payment evidence')}</h2></CardHeader>
      <CardContent><CapacityEvidencePreview orderId={order.id} evidence={order.evidence} /></CardContent>
    </Card> : null}
    <Card>
      <CardHeader><h2 className="font-display text-lg font-semibold">{t('Order summary')}</h2></CardHeader>
      <CardContent><CapacityOrderDetails order={order} /></CardContent>
    </Card>
  </>
}

export function CapacityOrderPage() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const { orderId } = useParams()
  const parsedId = capacityOrderIdSchema.safeParse(orderId)
  const order = useCapacityOrder(parsedId.success ? parsedId.data : null)
  const platformAdmin = isPlatformAdminUser(user)
  const usersLink = platformAdmin && order.data ? `/users?company=${order.data.tenant_id}` : '/users'

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header className="flex flex-col gap-3">
      <Button asChild variant="outline" className={`self-start ${capacityActionClass}`}><Link to={usersLink}><ArrowLeft aria-hidden="true" className="size-4" />{t('Back to Users')}</Link></Button>
      <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Extra accounts and agents order')}</h1>
    </header>
    {!parsedId.success ? <p role="alert" className="text-sm text-destructive">{t('This order link is invalid.')}</p> : <>
      {order.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('Loading order...')}</p> : null}
      {order.isError ? <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm text-destructive">{order.error instanceof ApiError
          ? order.error.status === 404 ? t('This order does not exist, or it belongs to another company.') : order.error.message
          : t('Could not load this order. Check your connection and retry.')}</p>
        <Button variant="outline" className={capacityActionClass} disabled={order.isFetching} onClick={() => { void order.refetch() }}>{t('Retry')}</Button>
      </div> : null}
      {order.data ? <CapacityOrderView key={order.data.id} order={order.data} readOnly={platformAdmin} /> : null}
    </>}
  </div>
}
