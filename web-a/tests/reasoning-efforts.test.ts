import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import {
  REASONING_EFFORTS,
  isReasoningEffort,
  reasoningEffortLabel,
  reasoningEffortsFor,
  availableReasoningEffort,
  reasoningEffortWrite,
  type ReasoningEffort,
} from '../src/lib/reasoning-efforts.ts'

test('selection narrowing accepts all seven presets and rejects unsupported values', () => {
  // Given
  const selections: readonly (string | undefined)[] = [
    'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', '', 'MAX', 'unknown', undefined,
  ]
  // When
  const accepted: ReasoningEffort[] = selections.filter(isReasoningEffort)
  // Then
  assert.deepEqual(accepted, ['none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
})

for (const effort of ['max', 'ultra']) {
  test(`form enum accepts ${effort} without transforming the saved value`, () => {
    // Given
    const schema = z.enum(REASONING_EFFORTS)
    // When
    const result = schema.safeParse(effort)
    // Then
    assert.deepEqual(result, { success: true, data: effort })
  })
}

for (const provider of ['openai', 'openai-response', 'azure', 'openrouter', 'anthropic', 'deepseek']) {
    test(`${provider} offers legacy presets only when provider metadata is missing`, () => {
      // Given / When
      const options = reasoningEffortsFor(provider)
      // Then
      assert.deepEqual(options, ['none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
    })
}

for (const provider of ['openai', 'grok', 'gemini', 'custom-provider']) {
  test(`${provider} hides the selector when explicit capabilities are empty`, () => {
    // Given / When
    const options = reasoningEffortsFor(provider, [])
    // Then
    assert.deepEqual(options, [])
  })
}

for (const [fromApi, selected, expected] of [
  [undefined, 'ultra', 'ultra'],
  [[], 'ultra', undefined],
  [['medium'], 'ultra', 'medium'],
  [['high', 'none'], undefined, 'none'],
  [['high', 'low'], undefined, 'high'],
  [['medium', 'high'], 'high', 'high'],
] as const) {
  test(`normalizes ${selected} against ${JSON.stringify(fromApi)} capabilities`, () => {
    // Given
    const efforts = reasoningEffortsFor('openai', fromApi)
    // When
    const selection = availableReasoningEffort(selected, efforts)
    // Then
    assert.equal(selection, expected)
  })
}

for (const [efforts, expected] of [
  [[], { reasoning_effort: null }],
  [['medium'], { reasoning_effort: 'medium' }],
  [['none', 'low'], { reasoning_effort: 'none' }],
] as const) {
  test(`submits only available effort for ${JSON.stringify(efforts)} capabilities`, () => {
    // Given: a stale value left by a previous provider/model
    const selected = 'ultra'
    // When
    const body = reasoningEffortWrite(selected, efforts)
    // Then
    assert.deepEqual(body, expected)
  })
}

test('capability transitions clear stale state and explicitly clear the saved effort', () => {
  // Given
  const capabilities = [undefined, [], ['medium'], [], undefined] as const
  let selected: string | undefined = 'ultra'
  // When
  const submissions = capabilities.map((fromApi) => {
    const efforts = reasoningEffortsFor('openai', fromApi)
    selected = availableReasoningEffort(selected, efforts)
    return reasoningEffortWrite(selected, efforts)
  })
  // Then
  assert.deepEqual(submissions, [
    { reasoning_effort: 'ultra' }, { reasoning_effort: null },
    { reasoning_effort: 'medium' }, { reasoning_effort: null }, { reasoning_effort: 'none' },
  ])
})

for (const [provider, expected] of [
  ['grok', ['none', 'low', 'medium', 'high', 'xhigh']],
  ['qwen', ['none', 'low', 'medium', 'high', 'xhigh']],
  ['gemini', ['none', 'low', 'medium', 'high']],
] as const) {
  test(`${provider} keeps its existing supported preset list`, () => {
    // Given / When
    const options = reasoningEffortsFor(provider)
    // Then
    assert.deepEqual(options, expected)
  })
}

for (const provider of ['openai', 'grok', 'gemini', 'custom-provider']) {
  for (const fromApi of [['ultra', 'low', 'max'], ['medium']]) {
    test(`${provider} preserves the API-provided ${fromApi.join('/')} choices and order`, () => {
      // Given / When
      const options = reasoningEffortsFor(provider, fromApi)
      // Then
      assert.deepEqual(options, fromApi)
    })
  }
}

for (const [effort, expected] of [
  ['none', 'None'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High'],
  ['xhigh', 'Extra High'], ['max', 'Max'], ['ultra', 'Ultra'], ['custom-effort', 'custom-effort'],
] as const) {
  test(`renders the ${effort} selection label as ${expected}`, () => {
    // Given / When
    const label = reasoningEffortLabel(effort)
    // Then
    assert.equal(label, expected)
  })
}
