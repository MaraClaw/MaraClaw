import assert from 'node:assert/strict'
import test from 'node:test'

import { quoteCapacity, quoteFromOverview, type CapacityQuoteInput } from '../src/lib/capacity-pricing.ts'
import { cappedOverview, uncappedOverview } from './capacity-fixtures.ts'

function purchase(remainingDays: number, daysInMonth: number, extras: { accounts?: number; agents?: number }, usdVndRate = 25_000) {
  return quoteCapacity({
    extraAccounts: extras.accounts ?? 0, extraAgents: extras.agents ?? 0, accountUnitUsd: 5, agentUnitUsd: 35,
    remainingDays, daysInMonth, usdVndRate,
  })
}

test('buying on the 1st costs the whole month', () => {
  assert.deepEqual(purchase(31, 31, { accounts: 1 }), { monthlyUsd: 5, amountUsdCents: 500, amountVnd: 125_000 })
})

test('buying on the last day of a 31-day month costs one thirty-first', () => {
  // 5 / 31 = 0.16129 USD is 16 cents; 0.16129 * 25000 / 1000 = 4.03 thousand VND rounds up to 5.
  assert.deepEqual(purchase(1, 31, { accounts: 1 }), { monthlyUsd: 5, amountUsdCents: 16, amountVnd: 5_000 })
})

test('a leap February has 29 days and a common one 28', () => {
  // 40 / 29 = 1.37931 USD: 138 cents; 1.37931 * 25500 / 1000 = 35.17 rounds up to 36.
  assert.deepEqual(purchase(1, 29, { accounts: 1, agents: 1 }, 25_500), { monthlyUsd: 40, amountUsdCents: 138, amountVnd: 36_000 })
  assert.equal(purchase(28, 28, { agents: 1 }).amountUsdCents, 3_500)
  // 35 / 28 = 1.25 USD exactly; 31 250 VND goes up to the next thousand.
  assert.deepEqual(purchase(1, 28, { agents: 1 }), { monthlyUsd: 35, amountUsdCents: 125, amountVnd: 32_000 })
})

test('mid-month proration rounds cents half up and dong up', () => {
  // 115 * 17 / 30 = 65.1666 USD: 6516.67 cents rounds to 6517; 65.1666 * 26000 / 1000 = 1694.33 rounds up to 1695.
  assert.deepEqual(purchase(17, 30, { accounts: 2, agents: 3 }, 26_000), { monthlyUsd: 115, amountUsdCents: 6_517, amountVnd: 1_695_000 })
  // The worked example of the pricing document: $65.17 at 25 000 VND per USD.
  assert.deepEqual(purchase(17, 30, { accounts: 2, agents: 3 }), { monthlyUsd: 115, amountUsdCents: 6_517, amountVnd: 1_630_000 })
})

test('cents round down below a half and an exact thousand is not rounded up', () => {
  assert.equal(purchase(17, 30, { accounts: 1 }).amountUsdCents, 283)
  assert.equal(purchase(31, 31, { accounts: 1 }, 24_000).amountVnd, 120_000)
})

test('the VND amount comes from the exact USD value, not from rounded cents', () => {
  // Rounded cents (2.83 USD) would give 2 830 000; the exact 2.8333 USD is 2 833 333 VND, up to 2 834 000.
  assert.equal(purchase(17, 30, { accounts: 1 }, 1_000_000).amountVnd, 2_834_000)
})

for (const [label, input, expected] of [
  ['mixed quantities on a 29-day month', { extraAccounts: 7, extraAgents: 3, accountUnitUsd: 4, agentUnitUsd: 49, remainingDays: 13, daysInMonth: 29, usdVndRate: 25_321 }, { monthlyUsd: 175, amountUsdCents: 7_845, amountVnd: 1_987_000 }],
  ['the largest order at the top rate', { extraAccounts: 10_000, extraAgents: 10_000, accountUnitUsd: 3, agentUnitUsd: 79, remainingDays: 31, daysInMonth: 31, usdVndRate: 1_000_000 }, { monthlyUsd: 820_000, amountUsdCents: 82_000_000, amountVnd: 820_000_000_000 }],
  ['a single account for three days', { extraAccounts: 1, extraAgents: 0, accountUnitUsd: 5, agentUnitUsd: 35, remainingDays: 3, daysInMonth: 30, usdVndRate: 25_999 }, { monthlyUsd: 5, amountUsdCents: 50, amountVnd: 13_000 }],
  ['many members in a 28-day month', { extraAccounts: 123, extraAgents: 456, accountUnitUsd: 4, agentUnitUsd: 49, remainingDays: 11, daysInMonth: 28, usdVndRate: 24_750 }, { monthlyUsd: 22_836, amountUsdCents: 897_129, amountVnd: 222_040_000 }],
  ['an odd rate that never lands on a thousand', { extraAccounts: 9_999, extraAgents: 1, accountUnitUsd: 5, agentUnitUsd: 35, remainingDays: 22, daysInMonth: 31, usdVndRate: 25_001 }, { monthlyUsd: 50_030, amountUsdCents: 3_550_516, amountVnd: 887_665_000 }],
  ['the lowest rate still costs one step', { extraAccounts: 1, extraAgents: 1, accountUnitUsd: 3, agentUnitUsd: 79, remainingDays: 1, daysInMonth: 31, usdVndRate: 1 }, { monthlyUsd: 82, amountUsdCents: 265, amountVnd: 1_000 }],
  ['a fractional dong total that float math would misround', { extraAccounts: 10_000, extraAgents: 0, accountUnitUsd: 5, agentUnitUsd: 35, remainingDays: 17, daysInMonth: 30, usdVndRate: 999_983 }, { monthlyUsd: 50_000, amountUsdCents: 2_833_333, amountVnd: 28_332_852_000 }],
] as const satisfies readonly (readonly [string, CapacityQuoteInput, ReturnType<typeof quoteCapacity>])[]) {
  test(`matches the engine for ${label}`, () => {
    assert.deepEqual(quoteCapacity(input), expected)
  })
}

for (const daysInMonth of [28, 29, 30, 31]) {
  test(`the amount never grows through a ${daysInMonth}-day month and stays a whole number of thousands`, () => {
    let previous = Number.POSITIVE_INFINITY
    for (let day = 1; day <= daysInMonth; day += 1) {
      const quote = purchase(daysInMonth - day + 1, daysInMonth, { accounts: 3, agents: 2 }, 25_321)
      assert.equal(Number.isInteger(quote.amountUsdCents), true)
      assert.equal(quote.amountVnd % 1000, 0)
      assert.ok(quote.amountVnd <= previous)
      previous = quote.amountVnd
    }
  })
}

test('a renewal is a purchase made on the 1st', () => {
  assert.deepEqual(purchase(31, 31, { accounts: 4, agents: 2 }, 25_100), quoteCapacity({
    extraAccounts: 4, extraAgents: 2, accountUnitUsd: 5, agentUnitUsd: 35, remainingDays: 31, daysInMonth: 31, usdVndRate: 25_100,
  }))
})

for (const [label, change] of [
  ['no extras', { extraAccounts: 0, extraAgents: 0 }],
  ['negative accounts', { extraAccounts: -1, extraAgents: 2 }],
  ['negative agents', { extraAccounts: 2, extraAgents: -1 }],
  ['fractional accounts', { extraAccounts: 1.5 }],
  ['more than 10 000 accounts', { extraAccounts: 10_001 }],
  ['more than 10 000 agents', { extraAgents: 10_001 }],
  ['a zero rate', { usdVndRate: 0 }],
  ['a fractional rate', { usdVndRate: 25_000.5 }],
  ['no remaining days', { remainingDays: 0 }],
  ['more remaining days than the month has', { remainingDays: 31, daysInMonth: 30 }],
  ['a zero-day month', { daysInMonth: 0 }],
] as const) {
  test(`rejects ${label}`, () => {
    const base: CapacityQuoteInput = { extraAccounts: 1, extraAgents: 1, accountUnitUsd: 5, agentUnitUsd: 35, remainingDays: 17, daysInMonth: 30, usdVndRate: 25_000 }
    assert.throws(() => quoteCapacity({ ...base, ...change }), RangeError)
  })
}

test('the preview uses the overview prices, clock and rate', () => {
  assert.deepEqual(quoteFromOverview(cappedOverview, 2, 3), { monthlyUsd: 115, amountUsdCents: 6_517, amountVnd: 1_630_000 })
})

test('there is no preview without extras, payment settings or prices', () => {
  assert.equal(quoteFromOverview(cappedOverview, 0, 0), null)
  assert.equal(quoteFromOverview({ ...cappedOverview, payments_configured: false, usd_vnd_rate: null }, 1, 0), null)
  assert.equal(quoteFromOverview({ ...cappedOverview, usd_vnd_rate: null }, 1, 0), null)
  assert.equal(quoteFromOverview({ ...cappedOverview, account_unit_usd: null }, 1, 0), null)
  assert.equal(quoteFromOverview(uncappedOverview, 1, 1), null)
})
