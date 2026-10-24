import assert from 'node:assert/strict'
import test from 'node:test'

import { formatMinorAmount, transferPolicyDraftSchema, transferPolicyToDraft } from '../src/pages/transfer-policy-form.ts'

const draft = { amount: '37.19', currency: 'EUR', instructions: 'Contact the platform team to arrange payment.\nInclude your request reference.' }

for (const [amount, minor] of [
  ['0', 0], ['0.00', 0], ['0.01', 1], ['0.29', 29], ['0.57', 57], ['1.01', 101],
  ['12.3', 1230], [' 0012.30 ', 1230], ['37.19', 3719], ['90071992547409.91', Number.MAX_SAFE_INTEGER],
] as const) {
  test(`decimal ${amount} converts exactly to ${minor} minor units`, () => {
    // Given
    const input = { ...draft, amount }
    // When
    const result = transferPolicyDraftSchema.parse(input)
    // Then
    assert.deepEqual(result, { fee_amount_minor: minor, currency: draft.currency, instructions: draft.instructions })
  })
}

for (const amount of ['', ' ', '-1', '+1', '1.005', '1e2', '1,000.00', 'NaN', 'Infinity', '90071992547409.92']) {
  test(`invalid decimal ${JSON.stringify(amount)} cannot become a policy payload`, () => {
    // Given / When
    const result = transferPolicyDraftSchema.safeParse({ ...draft, amount })
    // Then
    assert.equal(result.success, false)
  })
}

test('currency input normalizes before the full replacement payload is constructed', () => {
  // Given
  const input = { ...draft, currency: ' gbp ' }
  // When
  const result = transferPolicyDraftSchema.parse(input)
  // Then
  assert.deepEqual(result, { fee_amount_minor: 3719, currency: 'GBP', instructions: draft.instructions })
})

for (const currency of ['', 'US', 'USDD', 'U1D', '€UR']) {
  test(`invalid currency ${JSON.stringify(currency)} blocks submission`, () => {
    // Given / When
    const result = transferPolicyDraftSchema.safeParse({ ...draft, currency })
    // Then
    assert.equal(result.success, false)
  })
}

for (const instructions of ['', ' \n\t ', 'x'.repeat(4001)]) {
  test(`instructions with ${instructions.length} characters and invalid content block submission`, () => {
    // Given / When
    const result = transferPolicyDraftSchema.safeParse({ ...draft, instructions })
    // Then
    assert.equal(result.success, false)
  })
}

test('instructions accept the exact limit and preserve user line breaks', () => {
  // Given
  const instructions = `First line\n${'x'.repeat(3989)}`
  // When
  const result = transferPolicyDraftSchema.parse({ ...draft, instructions })
  // Then
  assert.equal(result.instructions, instructions)
})

for (const minor of [0, 1, 29, 3719, Number.MAX_SAFE_INTEGER]) {
  test(`server amount ${minor} populates a lossless editable decimal`, () => {
    // Given
    const policy = { fee_amount_minor: minor, currency: 'CAD', instructions: draft.instructions }
    // When
    const editable = transferPolicyToDraft(policy)
    // Then
    assert.deepEqual(transferPolicyDraftSchema.parse(editable), policy)
    assert.equal(editable.amount, formatMinorAmount(minor))
  })
}
