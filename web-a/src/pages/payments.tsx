import { useSearchParams } from 'react-router-dom'
import { useEffect, useRef, type KeyboardEvent } from 'react'

import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { useAuth } from '@/hooks/use-auth'
import { useCapacityAccess } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import { canReviewOrganizationAdmissions } from '@/lib/types/auth-guards'
import { CapacityPaymentSettingsSection } from '@/pages/capacity-payment-settings'
import { CapacityPaymentsPanel } from '@/pages/capacity-payments'
import { OrganizationAdmissionsPanel } from '@/pages/organization-admissions'
import { TransferPaymentsPanel } from '@/pages/transfer-payments'

const sections = [
  { id: 'transfers', label: 'Organization transfers', icon: 'users' },
  { id: 'capacity', label: 'Extra accounts and agents', icon: 'analytics' },
  { id: 'admissions', label: 'Organization admissions', icon: 'companies' },
  { id: 'settings', label: 'Payment settings', icon: 'settings' },
] as const satisfies readonly SectionRailItem[]

type PaymentsTab = (typeof sections)[number]['id']

const descriptions = {
  transfers: 'Check payments for requests to leave the default organization. Approval creates a destination admission request; it does not move the member.',
  capacity: 'Check bank transfers for extra accounts and agents. Approving a payment raises that company’s limits.',
  admissions: 'Review requests for a new organization, the commercial terms quoted at submission and the payment evidence uploaded with them.',
  settings: null,
} as const

export function PaymentsPage() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const { canManageSettings } = useCapacityAccess()
  const [searchParams, setSearchParams] = useSearchParams()
  const access: Record<PaymentsTab, boolean> = {
    transfers: true, capacity: true, admissions: canReviewOrganizationAdmissions(user), settings: canManageSettings,
  }
  const visibleSections = sections.filter((item) => access[item.id])
  const tab = visibleSections.find((item) => item.id === searchParams.get('tab'))?.id ?? 'transfers'
  const description = descriptions[tab]
  const focusSelectedTab = useRef(false)

  useEffect(() => {
    if (!focusSelectedTab.current) return
    focusSelectedTab.current = false
    document.getElementById(`payments-tab-${tab}`)?.focus()
  }, [tab])

  function setTab(next: PaymentsTab) {
    const copy = new URLSearchParams(searchParams)
    if (next === 'transfers') copy.delete('tab')
    else copy.set('tab', next)
    setSearchParams(copy, { replace: true })
  }

  function selectTab(next: PaymentsTab) {
    focusSelectedTab.current = next !== tab
    setTab(next)
  }

  function onRailKeyDown(event: KeyboardEvent<HTMLElement>) {
    const order = visibleSections.map((item) => item.id)
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
      selectTab(order.at(-1) ?? 'transfers')
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_1fr] overflow-hidden md:grid-cols-[6rem_1fr] md:grid-rows-[auto_1fr]">
      <div className="min-w-0 bg-background px-6 py-4 md:col-start-2 md:row-start-1">
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Payments')}</h1>
        {description ? <p className="mt-2 text-muted-foreground">{t(description)}</p> : null}
      </div>
      <SectionRail
        items={visibleSections.map((item) => ({ ...item, label: t(item.label) }))}
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
          className: 'w-20 md:w-full',
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

        {tab === 'admissions' ? (
          <div id="payments-panel-admissions" role="tabpanel" aria-labelledby="payments-tab-admissions">
            <OrganizationAdmissionsPanel />
          </div>
        ) : null}

        {tab === 'settings' ? (
          <div id="payments-panel-settings" role="tabpanel" aria-labelledby="payments-tab-settings" className="admission-content mx-auto w-full max-w-3xl">
            <CapacityPaymentSettingsSection />
          </div>
        ) : null}
      </div>
    </div>
  )
}
