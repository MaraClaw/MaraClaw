import assert from 'node:assert/strict'
import test from 'node:test'

import { capacityDecisionDraftSchema, capacityOrderDraftSchema, capacityPaymentSettingsDraftSchema, evidenceFileProblem } from '../src/lib/capacity-forms.ts'
import { formatBillingMonth, formatCalendarDate, formatFileSize, formatUsd, formatUsdCents, formatVnd } from '../src/lib/capacity-format.ts'
import { canCancelOrder, canSubmitEvidence, isDecidable, isProrated, isRenewalLapsed, vietnamToday } from '../src/lib/capacity-order-state.ts'
import { vietnamese } from '../src/lib/translations.ts'
import { purchaseOrder, renewalOrder, submittedOrder } from './capacity-fixtures.ts'

const issues = (result: { success: boolean; error?: { issues: readonly { message: string; path: PropertyKey[] }[] } }) =>
  result.error?.issues.map((issue) => [issue.path.join('.'), issue.message]) ?? []

test('the order draft turns whole-number text into counts', () => {
  assert.deepEqual(capacityOrderDraftSchema.parse({ extraAccounts: ' 2 ', extraAgents: '0003' }), { extraAccounts: 2, extraAgents: 3 })
  assert.deepEqual(capacityOrderDraftSchema.parse({ extraAccounts: '10000', extraAgents: '0' }), { extraAccounts: 10_000, extraAgents: 0 })
})

for (const text of ['', '  ', '1.5', '-1', '1e3', '10001', '99999999999999999999', 'two', '1,000']) {
  test(`the order draft refuses "${text}" as a count`, () => {
    const result = capacityOrderDraftSchema.safeParse({ extraAccounts: text, extraAgents: '1' })
    assert.equal(result.success, false)
    assert.deepEqual(issues(result), [['extraAccounts', 'Enter a whole number from 0 to 10,000.']])
  })
}

test('the order draft needs at least one extra and blames the second field', () => {
  assert.deepEqual(issues(capacityOrderDraftSchema.safeParse({ extraAccounts: '0', extraAgents: '0' })), [['extraAgents', 'Order at least one extra account or agent.']])
})

test('every message a form can show has a Vietnamese translation', () => {
  for (const message of ['Enter a whole number from 0 to 10,000.', 'Order at least one extra account or agent.', 'Enter a value.', 'Use 120 characters or fewer.',
    'Remove line breaks and other control characters.', 'Enter a whole number from 1 to 1,000,000.', 'Use 1,000 characters or fewer.',
    'Choose a PNG, JPEG, WebP or PDF file.', 'This file is empty. Choose another file.', 'This file is larger than 10 MiB. Choose a smaller file.',
    'Enter a note explaining the rejection.', 'Choose the file that shows your transfer.']) {
    assert.ok(Object.hasOwn(vietnamese, message), message)
  }
})

test('a decision note counts code points and is trimmed', () => {
  assert.equal(capacityDecisionDraftSchema.parse({ note: '  Amount does not match.  ' }).note, 'Amount does not match.')
  assert.equal(capacityDecisionDraftSchema.safeParse({ note: '\u{1F4B8}'.repeat(1000) }).success, true)
  assert.deepEqual(issues(capacityDecisionDraftSchema.safeParse({ note: 'x'.repeat(1001) })), [['note', 'Use 1,000 characters or fewer.']])
})

const settings = { bank_name: ' Example Bank ', account_number: '0123456789', branch_name: 'District 1 Branch', usd_vnd_rate: ' 25000 ' }

test('the settings draft trims the texts and reads the rate as a whole number', () => {
  assert.deepEqual(capacityPaymentSettingsDraftSchema.parse(settings), {
    bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'District 1 Branch', usd_vnd_rate: 25_000,
  })
})

for (const [label, change, message] of [
  ['a blank bank name', { bank_name: '   ' }, 'Enter a value.'],
  ['a 121-character branch', { branch_name: 'b'.repeat(121) }, 'Use 120 characters or fewer.'],
  ['a line break in the account number', { account_number: '0123\n456' }, 'Remove line breaks and other control characters.'],
  ['a zero-width format character', { bank_name: 'Bank\u200BName' }, 'Remove line breaks and other control characters.'],
  ['a fractional rate', { usd_vnd_rate: '25000.5' }, 'Enter a whole number from 1 to 1,000,000.'],
  ['a zero rate', { usd_vnd_rate: '0' }, 'Enter a whole number from 1 to 1,000,000.'],
  ['a rate above one million', { usd_vnd_rate: '1000001' }, 'Enter a whole number from 1 to 1,000,000.'],
  ['a blank rate', { usd_vnd_rate: '' }, 'Enter a whole number from 1 to 1,000,000.'],
] as const) {
  test(`the settings draft refuses ${label}`, () => {
    const result = capacityPaymentSettingsDraftSchema.safeParse({ ...settings, ...change })
    assert.equal(result.success, false)
    assert.deepEqual(issues(result).map(([, text]) => text), [message])
  })
}

test('the settings draft accepts the one-million rate and 120-character texts', () => {
  assert.equal(capacityPaymentSettingsDraftSchema.safeParse({ ...settings, usd_vnd_rate: '1000000', bank_name: 'b'.repeat(120) }).success, true)
})

const tenMiB = 10 * 1024 * 1024

for (const [label, file, expected] of [
  ['a PNG', { name: 'receipt.png', type: 'image/png', size: 1024 }, null],
  ['a JPEG, WebP and PDF', { name: 'r.jpg', type: 'image/jpeg', size: 1 }, null],
  ['a WebP', { name: 'r.webp', type: 'image/webp', size: 1 }, null],
  ['a PDF', { name: 'receipt.pdf', type: 'application/pdf', size: 1024 }, null],
  ['exactly 10 MiB', { name: 'receipt.png', type: 'image/png', size: tenMiB }, null],
  ['a file with no type but a known extension', { name: 'RECEIPT.JPEG', type: '', size: 10 }, null],
  ['a generic binary type with a known extension', { name: 'receipt.pdf', type: 'application/octet-stream', size: 10 }, null],
  ['one byte over 10 MiB', { name: 'receipt.png', type: 'image/png', size: tenMiB + 1 }, 'This file is larger than 10 MiB. Choose a smaller file.'],
  ['an empty file', { name: 'receipt.png', type: 'image/png', size: 0 }, 'This file is empty. Choose another file.'],
  ['a text file', { name: 'notes.txt', type: 'text/plain', size: 10 }, 'Choose a PNG, JPEG, WebP or PDF file.'],
  ['a GIF', { name: 'receipt.gif', type: 'image/gif', size: 10 }, 'Choose a PNG, JPEG, WebP or PDF file.'],
  ['a document whose type lies about its name', { name: 'receipt.png', type: 'text/html', size: 10 }, 'Choose a PNG, JPEG, WebP or PDF file.'],
  ['an unknown type without an extension', { name: 'receipt', type: '', size: 10 }, 'Choose a PNG, JPEG, WebP or PDF file.'],
] as const) {
  test(`the client check ${expected === null ? 'accepts' : 'refuses'} ${label}`, () => {
    assert.equal(evidenceFileProblem(file), expected)
  })
}

test('VND is grouped for the locale and USD keeps two decimals', () => {
  assert.equal(formatVnd(1_250_000, 'en'), '1,250,000 VND')
  assert.equal(formatVnd(1_250_000, 'vi'), '1.250.000 VND')
  assert.equal(formatUsdCents(6_517, 'en'), '$65.17')
  assert.equal(formatUsdCents(500, 'en'), '$5.00')
  assert.equal(formatUsd(115, 'en'), '$115.00')
  assert.match(formatUsdCents(6_517, 'vi'), /65,17/)
})

test('calendar days keep their day in every time zone', () => {
  assert.equal(formatCalendarDate('2026-12-07', 'en'), 'December 7, 2026')
  assert.match(formatCalendarDate('2026-12-07', 'vi'), /7.*12.*2026/)
  assert.equal(formatBillingMonth('2026-12-01', 'en'), 'December 2026')
  assert.equal(formatBillingMonth('2026-11-14', 'en'), 'November 2026')
})

test('file sizes use KiB and MiB with at most one decimal', () => {
  assert.equal(formatFileSize(512, 'en'), '512 B')
  assert.equal(formatFileSize(20_480, 'en'), '20 KiB')
  assert.equal(formatFileSize(1_572_864, 'en'), '1.5 MiB')
  assert.equal(formatFileSize(10 * 1024 * 1024, 'en'), '10 MiB')
})

test('the Vietnam calendar day turns over at 17:00 UTC', () => {
  assert.equal(vietnamToday(new Date('2026-11-14T16:59:59Z')), '2026-11-14')
  assert.equal(vietnamToday(new Date('2026-11-14T17:00:00Z')), '2026-11-15')
  assert.equal(vietnamToday(new Date('2026-12-31T17:30:00Z')), '2027-01-01')
})

test('an unpaid renewal lapses the day after grace_until', () => {
  assert.equal(isRenewalLapsed(renewalOrder, '2026-12-01'), false)
  assert.equal(isRenewalLapsed(renewalOrder, '2026-12-07'), false)
  assert.equal(isRenewalLapsed(renewalOrder, '2026-12-08'), true)
  assert.equal(isRenewalLapsed({ ...renewalOrder, status: 'submitted' }, '2026-12-20'), false)
  assert.equal(isRenewalLapsed({ ...renewalOrder, status: 'approved' }, '2026-12-20'), false)
  assert.equal(isRenewalLapsed(purchaseOrder, '2026-12-20'), false)
})

test('order actions follow the status and kind', () => {
  assert.equal(canSubmitEvidence(purchaseOrder), true)
  assert.equal(canSubmitEvidence(submittedOrder), false)
  assert.equal(canCancelOrder(purchaseOrder), true)
  assert.equal(canCancelOrder(renewalOrder), false)
  assert.equal(canCancelOrder(submittedOrder), false)
  assert.equal(isDecidable(submittedOrder), true)
  assert.equal(isDecidable(purchaseOrder), false)
  assert.equal(isDecidable({ ...submittedOrder, status: 'approved' }), false)
})

test('only a purchase made part-way through a month is prorated', () => {
  assert.equal(isProrated(purchaseOrder), true)
  assert.equal(isProrated({ ...purchaseOrder, days_charged: 30 }), false)
  assert.equal(isProrated(renewalOrder), false)
})
