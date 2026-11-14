import assert from 'node:assert/strict'
import test from 'node:test'

import { createCompanySchema } from '../src/lib/company-form-schema.ts'
import { vietnamese } from '../src/lib/translations.ts'

const draft = { name: ' Acme ', admin_email: ' Admin@Example.test ', admin_display_name: '', admin_password: 'secret-1' }

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

test('every message the company form can show has a Vietnamese translation', () => {
  for (const message of ['Enter a company name', 'Name is too long', 'Choose a plan', 'Enter the org admin email',
    'Enter a valid email address', 'Email is too long', 'Display name is too long', 'Use at least 6 characters',
    'Password is too long', '{price}/month · {accounts} accounts · {agents} agents']) {
    assert.ok(Object.hasOwn(vietnamese, message), message)
  }
})
