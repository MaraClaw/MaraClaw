export const MAX_CAPACITY_EXTRAS = 10_000
export const VND_STEP = 1000

export type CapacityQuoteInput = {
  readonly extraAccounts: number
  readonly extraAgents: number
  readonly accountUnitUsd: number
  readonly agentUnitUsd: number
  readonly remainingDays: number
  readonly daysInMonth: number
  readonly usdVndRate: number
}

export type CapacityQuote = {
  /** What the extras cost for a whole month: the price from next month on. */
  readonly monthlyUsd: number
  readonly amountUsdCents: number
  readonly amountVnd: number
}

function whole(name: string, value: number, min: number, max = Number.MAX_SAFE_INTEGER): bigint {
  if (!Number.isInteger(value) || value < min || value > max) throw new RangeError(`${name} must be a whole number from ${min} to ${max}`)
  return BigInt(value)
}

/**
 * The server's formula (`engine/app/services/capacity_pricing.py`) in integers, so the preview never drifts:
 * `usd_exact = monthly_usd * remaining / N`, cents round half up, VND rounds up to the next thousand from the exact
 * USD value (not from the rounded cents).
 */
export function quoteCapacity(input: CapacityQuoteInput): CapacityQuote {
  const accounts = whole('extraAccounts', input.extraAccounts, 0, MAX_CAPACITY_EXTRAS)
  const agents = whole('extraAgents', input.extraAgents, 0, MAX_CAPACITY_EXTRAS)
  if (accounts + agents === 0n) throw new RangeError('A capacity order needs at least one extra account or agent')
  const days = whole('daysInMonth', input.daysInMonth, 1)
  const remaining = whole('remainingDays', input.remainingDays, 1, input.daysInMonth)
  const rate = whole('usdVndRate', input.usdVndRate, 1)
  const monthlyUsd = accounts * whole('accountUnitUsd', input.accountUnitUsd, 0) + agents * whole('agentUnitUsd', input.agentUnitUsd, 0)
  const proratedUsd = monthlyUsd * remaining
  const amountUsdCents = (200n * proratedUsd + days) / (2n * days)
  const vndDenominator = days * BigInt(VND_STEP)
  const amountVnd = ((proratedUsd * rate + vndDenominator - 1n) / vndDenominator) * BigInt(VND_STEP)
  return { monthlyUsd: Number(monthlyUsd), amountUsdCents: Number(amountUsdCents), amountVnd: Number(amountVnd) }
}

type OverviewPrices = {
  readonly capped: boolean
  readonly payments_configured: boolean
  readonly account_unit_usd: number | null
  readonly agent_unit_usd: number | null
  readonly usd_vnd_rate: number | null
  readonly remaining_days: number
  readonly days_in_month: number
}

/** The quote a purchase made now would get, or `null` when the company cannot buy or no rate is configured. */
export function quoteFromOverview(overview: OverviewPrices, extraAccounts: number, extraAgents: number): CapacityQuote | null {
  if (!overview.capped || !overview.payments_configured) return null
  const { account_unit_usd: accountUnitUsd, agent_unit_usd: agentUnitUsd, usd_vnd_rate: usdVndRate } = overview
  if (accountUnitUsd === null || agentUnitUsd === null || usdVndRate === null) return null
  if (extraAccounts + extraAgents === 0) return null
  return quoteCapacity({
    extraAccounts, extraAgents, accountUnitUsd, agentUnitUsd, usdVndRate,
    remainingDays: overview.remaining_days, daysInMonth: overview.days_in_month,
  })
}
