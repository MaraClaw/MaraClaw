const BASE_REASONING_EFFORTS = ['none', 'low', 'medium', 'high', 'xhigh'] as const

export const REASONING_EFFORTS = [...BASE_REASONING_EFFORTS, 'max', 'ultra'] as const

export type ReasoningEffort = (typeof REASONING_EFFORTS)[number]

const PROVIDER_REASONING_EFFORTS: Readonly<Record<string, readonly ReasoningEffort[]>> = {
  openai: REASONING_EFFORTS,
  'openai-response': REASONING_EFFORTS,
  azure: REASONING_EFFORTS,
  grok: BASE_REASONING_EFFORTS,
  openrouter: REASONING_EFFORTS,
  anthropic: REASONING_EFFORTS,
  gemini: ['none', 'low', 'medium', 'high'],
  deepseek: REASONING_EFFORTS,
  qwen: BASE_REASONING_EFFORTS,
}

const REASONING_EFFORT_LABELS: Readonly<Record<ReasoningEffort, string>> = {
  none: 'None',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra High',
  max: 'Max',
  ultra: 'Ultra',
}

export function isReasoningEffort(effort: string | undefined): effort is ReasoningEffort {
  return REASONING_EFFORTS.some((candidate) => candidate === effort)
}

export function reasoningEffortLabel(effort: string): string {
  return isReasoningEffort(effort) ? REASONING_EFFORT_LABELS[effort] : effort
}

export function reasoningEffortsFor(provider: string, fromApi?: readonly string[]): readonly string[] {
  if (fromApi !== undefined) return fromApi
  return [...(PROVIDER_REASONING_EFFORTS[provider] ?? REASONING_EFFORTS)]
}

export function availableReasoningEffort(
  selected: string | undefined,
  efforts: readonly string[],
): ReasoningEffort | undefined {
  const available = efforts.filter(isReasoningEffort)
  if (isReasoningEffort(selected) && available.includes(selected)) return selected
  return available.includes('none') ? 'none' : available[0]
}

export function reasoningEffortWrite(selected: string | undefined, efforts: readonly string[]) {
  const effort = availableReasoningEffort(selected, efforts)
  return { reasoning_effort: effort ?? null }
}
