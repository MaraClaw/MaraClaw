import assert from 'node:assert/strict'
import test from 'node:test'

import { createCompanySchema } from '../src/lib/company-form-schema.ts'
import { DEFAULT_PHONE_COUNTRY, toE164 } from '../src/lib/phone.ts'
import { vietnamese } from '../src/lib/translations.ts'

const draft = {
  name: ' Acme ', phone_country: 'VN', phone_number: '091 234 5678', admin_email: ' Admin@Example.test ',
  admin_display_name: '', admin_password: 'secret-1',
}

const issues = (result: { success: boolean; error?: { issues: readonly { message: string; path: PropertyKey[] }[] } }) =>
  result.error?.issues.map((issue) => [issue.path.join('.'), issue.message]) ?? []

for (const tier of ['pro', 'max', 'ultra'] as const) {
  test(`the company form accepts the ${tier} plan`, () => {
    assert.equal(createCompanySchema.parse({ ...draft, tier }).tier, tier)
  })
}

for (const tier of [undefined, null, '', 'gold', 'PRO']) {
  test(`the company form needs a known plan, not ${JSON.stringify(tier) ?? 'nothing'}`, () => {
    assert.deepEqual(issues(createCompanySchema.safeParse({ ...draft, tier })), [['tier', 'Choose a plan']])
  })
}

test('the company form defaults to Vietnam and accepts a Vietnamese national number', () => {
  assert.equal(DEFAULT_PHONE_COUNTRY, 'VN')
  assert.equal(createCompanySchema.parse({ ...draft, tier: 'pro' }).phone_country, 'VN')
})

for (const phone_number of ['', '   ']) {
  test(`the company form needs a phone number, not ${JSON.stringify(phone_number)}`, () => {
    assert.deepEqual(issues(createCompanySchema.safeParse({ ...draft, tier: 'pro', phone_number })),
      [['phone_number', 'Enter the phone number']])
  })
}

for (const phone_number of ['abc', '12345', '1234567', '0212345678']) {
  test(`the company form rejects ${JSON.stringify(phone_number)} as a Vietnamese number`, () => {
    assert.deepEqual(issues(createCompanySchema.safeParse({ ...draft, tier: 'pro', phone_number })),
      [['phone_number', 'Enter a valid phone number']])
  })
}

test('the company form checks the number against the chosen country and still reports an unchosen plan', () => {
  assert.equal(createCompanySchema.safeParse({ ...draft, tier: 'pro', phone_country: 'US', phone_number: '202 555 0123' }).success, true)
  assert.deepEqual(issues(createCompanySchema.safeParse({ ...draft, tier: 'pro', phone_number: '202 555 0123' })),
    [['phone_number', 'Enter a valid phone number']])
  assert.deepEqual(issues(createCompanySchema.safeParse({ ...draft, tier: undefined, phone_number: '12345' })),
    [['tier', 'Choose a plan'], ['phone_number', 'Enter a valid phone number']])
})

test('phone numbers become compact E.164 for the engine', () => {
  assert.equal(toE164('VN', '091 234 5678'), '+84912345678')
  assert.equal(toE164('VN', '912345678'), '+84912345678')
  assert.equal(toE164('VN', '+1 202 555 0123'), '+12025550123')
  assert.equal(toE164('VN', '12345'), null)
})

test('every message the company form can show has a Vietnamese translation', () => {
  for (const message of ['Enter a company name', 'Name is too long', 'Enter the phone number', 'Enter a valid phone number',
    'Choose a plan', 'Enter the org admin email', 'Enter a valid email address', 'Email is too long',
    'Display name is too long', 'Use at least 6 characters', 'Password is too long',
    '{price}/month · {accounts} accounts · {agents} agents']) {
    assert.ok(Object.hasOwn(vietnamese, message), message)
  }
})
