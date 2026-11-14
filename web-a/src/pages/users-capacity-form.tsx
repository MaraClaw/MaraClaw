import { zodResolver } from '@hookform/resolvers/zod'
import { useId } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { Link, useNavigate } from 'react-router-dom'

import { capacityActionClass } from '@/components/capacity/capacity-labels'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useCapacityOrderCreate } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import { capacityConflict, type CapacityOrder, type CapacityOverview } from '@/lib/capacity-api'
import { capacityGuardKey } from '@/lib/capacity-errors'
import { capacityOrderDraftSchema, type CapacityOrderDraft, type CapacityOrderValues } from '@/lib/capacity-forms'
import { formatBillingMonth, formatUsd, formatUsdCents, formatVnd } from '@/lib/capacity-format'
import { quoteFromOverview } from '@/lib/capacity-pricing'
import { ApiError } from '@/lib/http'

export function CapacityBuyForm({ overview, openPurchase }: { readonly overview: CapacityOverview; readonly openPurchase: CapacityOrder | null }) {
  const { t, locale, number, owned } = useTranslation()
  const id = useId()
  const navigate = useNavigate()
  const create = useCapacityOrderCreate()
  const form = useForm<CapacityOrderDraft, unknown, CapacityOrderValues>({
    resolver: zodResolver(capacityOrderDraftSchema), mode: 'onTouched', defaultValues: { extraAccounts: '0', extraAgents: '0' },
  })
  const draft = capacityOrderDraftSchema.safeParse(useWatch({ control: form.control }))
  const quote = draft.success ? quoteFromOverview(overview, draft.data.extraAccounts, draft.data.extraAgents) : null
  const { errors } = form.formState
  const blocked = !overview.payments_configured || openPurchase !== null
  const conflict = capacityConflict(create.error)
  const openOrderId = conflict?.code === 'purchase_already_open' ? conflict.orderId : null
  const accountPrice = overview.account_unit_usd === null ? '' : formatUsd(overview.account_unit_usd, locale)
  const agentPrice = overview.agent_unit_usd === null ? '' : formatUsd(overview.agent_unit_usd, locale)
  const month = formatBillingMonth(overview.today, locale)

  const refused = capacityGuardKey(create.error)
  const failure = refused !== null ? t(refused) : conflict?.code === 'payments_not_configured'
    ? t('Payments are not set up yet. Ask the platform admin to add the bank details.')
    : conflict?.code === 'purchase_already_open'
      ? t('You already have an open order. Finish or cancel it before placing another.')
      : conflict?.code === 'not_eligible'
        ? t('This company’s plan cannot buy extra accounts and agents.')
        : create.error instanceof ApiError ? create.error.message : t('Could not place the order. Check your connection and try again.')

  const fields = [
    { name: 'extraAccounts', label: 'Extra accounts', help: t('Each extra account costs {price} a month.', { price: accountPrice }) },
    { name: 'extraAgents', label: 'Extra agents', help: t('Each extra agent costs {price} a month.', { price: agentPrice }) },
  ] as const

  return <Card>
    <CardHeader>
      <h3 className="font-display text-base font-semibold">{t('Buy extra accounts and agents')}</h3>
      <p className="text-sm leading-relaxed text-muted-foreground">{t('Enter how many you need. You pay by bank transfer, and they start once a platform admin approves the payment.')}</p>
    </CardHeader>
    <CardContent className="flex flex-col gap-4">
      {!overview.payments_configured ? <p role="status" className="text-sm leading-relaxed">{t('Payments are not set up yet. Ask the platform admin to add the bank details, then come back.')}</p> : null}
      {openPurchase !== null ? <p role="status" className="text-sm leading-relaxed">
        {t('You have an open order. Finish or cancel it before placing another.')}{' '}
        <Link className="text-primary underline underline-offset-4" to={`/capacity-orders/${openPurchase.id}`}>{t('Open payment page')}</Link>
      </p> : null}
      <form noValidate className="flex flex-col gap-4" aria-busy={create.isPending} onSubmit={form.handleSubmit(({ extraAccounts, extraAgents }) => {
        if (blocked || create.isPending) return
        create.mutate({ extra_accounts: extraAccounts, extra_agents: extraAgents }, { onSuccess: (order) => { void navigate(`/capacity-orders/${order.id}`) } })
      })}>
        <fieldset disabled={blocked || create.isPending} className="flex min-w-0 flex-col gap-4 disabled:opacity-60">
          <legend className="sr-only">{t('Extra accounts and agents to buy')}</legend>
          <div className="grid gap-4 sm:grid-cols-2">
            {fields.map(({ name, label, help }) => <div key={name} className="flex flex-col gap-2">
              <Label htmlFor={`${id}-${name}`}>{t(label)}</Label>
              <Input id={`${id}-${name}`} inputMode="numeric" autoComplete="off" aria-required="true" aria-invalid={Boolean(errors[name])}
                aria-describedby={`${id}-${name}-help${errors[name] ? ` ${id}-${name}-error` : ''}`} {...form.register(name)} />
              <p id={`${id}-${name}-help`} className="text-xs text-muted-foreground">{help}</p>
              {errors[name] ? <p id={`${id}-${name}-error`} role="alert" className="text-sm text-destructive">{owned(errors[name].message)}</p> : null}
            </div>)}
          </div>
        </fieldset>
        <div aria-live="polite" className="rounded-xl border border-border bg-background p-4 text-sm leading-relaxed">
          {quote !== null ? <div className="flex flex-col gap-1">
            <p className="text-muted-foreground">{t('Amount to pay now')}</p>
            <p className="font-display text-2xl font-semibold tabular-nums">{formatVnd(quote.amountVnd, locale)}</p>
            <p className="tabular-nums text-muted-foreground">{t('{usd} at {rate} VND per USD', { usd: formatUsdCents(quote.amountUsdCents, locale), rate: number(overview.usd_vnd_rate ?? 0) })}</p>
            <p className="tabular-nums">{t('Prorated: {remaining} of {days} days left in {month}', { remaining: number(overview.remaining_days), days: number(overview.days_in_month), month })}</p>
            <p className="tabular-nums">{t('From next month: {price} a month', { price: formatUsd(quote.monthlyUsd, locale) })}</p>
            {draft.success && overview.account_limit !== null && overview.agent_limit !== null
              ? <p className="tabular-nums text-muted-foreground">{t('After approval, your limits are {accounts} accounts and {agents} agents.', { accounts: number(overview.account_limit + draft.data.extraAccounts), agents: number(overview.agent_limit + draft.data.extraAgents) })}</p> : null}
          </div> : <p className="text-muted-foreground">{overview.payments_configured ? t('Enter at least one extra account or agent to see the price.') : t('The price appears here once payments are set up.')}</p>}
        </div>
        {create.isError ? <div role="alert" className="flex flex-col items-start gap-2 text-sm text-destructive">
          <p>{failure}</p>
          {openOrderId !== null ? <Link className="text-primary underline underline-offset-4" to={`/capacity-orders/${openOrderId}`}>{t('Open payment page')}</Link> : null}
        </div> : null}
        <Button type="submit" className={`self-start ${capacityActionClass}`} disabled={blocked || create.isPending} aria-busy={create.isPending}>{t(create.isPending ? 'Placing order...' : 'Place order')}</Button>
      </form>
    </CardContent>
  </Card>
}
