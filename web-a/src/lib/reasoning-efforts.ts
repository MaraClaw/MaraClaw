const BASE_REASONING_EFFORTS = ['none', 'low', 'medium', 'high', 'xhigh'] as const

/** Always available in the Models form, even if a stale engine omits it. */
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

export function reasoningEffortsFor(provider: string, fromApi?: string[]): string[] {
  if (fromApi && fromApi.length > 0) return fromApi
  return [...(PROVIDER_REASONING_EFFORTS[provider] ?? REASONING_EFFORTS)]
}
