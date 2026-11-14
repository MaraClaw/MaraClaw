import { useSearchParams } from 'react-router-dom'
import type { KeyboardEvent } from 'react'

import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { useTranslation } from '@/hooks/use-translation'
import { CapacityPaymentsPanel } from '@/pages/capacity-payments'
import { TransferPaymentsPanel } from '@/pages/transfer-payments'

const sections = [
  { id: 'transfers', label: 'Organization transfers', icon: 'users' },
  { id: 'capacity', label: 'Extra accounts and agents', icon: 'analytics' },
] as const satisfies readonly SectionRailItem[]

type PaymentsTab = (typeof sections)[number]['id']

export function PaymentsPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab: PaymentsTab = searchParams.get('tab') === 'capacity' ? 'capacity' : 'transfers'

  function setTab(next: PaymentsTab) {
    const copy = new URLSearchParams(searchParams)
    if (next === 'capacity') copy.set('tab', 'capacity')
    else copy.delete('tab')
    setSearchParams(copy, { replace: true })
  }

  function selectTab(next: PaymentsTab) {
    setTab(next)
    queueMicrotask(() => document.getElementById(`payments-tab-${next}`)?.focus())
  }

  function onRailKeyDown(event: KeyboardEvent<HTMLElement>) {
    const order: PaymentsTab[] = ['transfers', 'capacity']
    const index = order.indexOf(tab)
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault()
      selectTab(order[(index + 1) % order.length] ?? 'transfers')
    }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault()
      selectTab(order[(index - 1 + order.length) % order.length] ?? 'transfers')
    }
    if (event.key === 'Home') {
      event.preventDefault()
      selectTab('transfers')
    }
    if (event.key === 'End') {
      event.preventDefault()
      selectTab('capacity')
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_1fr] overflow-hidden md:grid-cols-[6rem_1fr] md:grid-rows-[auto_1fr]">
      <div className="min-w-0 bg-background px-6 py-4 md:col-start-2 md:row-start-1">
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Payments')}</h1>
        <p className="mt-2 text-muted-foreground">
          {tab === 'capacity'
            ? t('Check bank transfers for extra accounts and agents. Approving a payment raises that company’s limits.')
            : t('Check payments for requests to leave the default organization. Approval creates a destination admission request; it does not move the member.')}
        </p>
      </div>
      <SectionRail
        items={sections.map((item) => ({ ...item, label: t(item.label) }))}
        active={tab}
        onSelect={selectTab}
        label={t('Payment sections')}
        role="tablist"
        onKeyDown={onRailKeyDown}
        className="md:col-start-1 md:row-start-1 md:row-span-2"
        itemProps={(item, isActive) => ({
          role: 'tab',
          id: `payments-tab-${item.id}`,
          'aria-selected': isActive,
          'aria-controls': `payments-panel-${item.id}`,
          tabIndex: isActive ? 0 : -1,
        })}
      />
      <div className="min-h-0 overflow-y-auto overscroll-y-contain p-6 md:col-start-2 md:row-start-2">
        {tab === 'transfers' ? (
          <div id="payments-panel-transfers" role="tabpanel" aria-labelledby="payments-tab-transfers">
            <TransferPaymentsPanel />
          </div>
        ) : null}

        {tab === 'capacity' ? (
          <div id="payments-panel-capacity" role="tabpanel" aria-labelledby="payments-tab-capacity">
            <CapacityPaymentsPanel />
          </div>
        ) : null}
      </div>
    </div>
  )
}
