import type { CapacityOrder } from './capacity-api.ts'

type OrderState = Pick<CapacityOrder, 'kind' | 'status'>

export function canSubmitEvidence(order: OrderState): boolean {
  return order.status === 'awaiting_payment'
}

export function canCancelOrder(order: OrderState): boolean {
  return order.kind === 'purchase' && order.status === 'awaiting_payment'
}

export function isDecidable(order: OrderState): boolean {
  return order.status === 'submitted'
}

/** An unpaid renewal counts toward the caps through `grace_until` (the 7th) and stops counting the day after. */
export function isRenewalLapsed(order: OrderState & Pick<CapacityOrder, 'grace_until'>, today: string): boolean {
  return order.kind === 'renewal' && order.status === 'awaiting_payment' && order.grace_until !== null && today > order.grace_until
}

export function vietnamToday(now: Date = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Ho_Chi_Minh', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now)
}

export function isProrated(order: Pick<CapacityOrder, 'kind' | 'days_charged' | 'days_in_month'>): boolean {
  return order.kind === 'purchase' && order.days_charged < order.days_in_month
}
