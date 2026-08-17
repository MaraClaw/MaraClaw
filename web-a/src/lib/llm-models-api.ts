import { apiRequest } from '@/lib/http'

export type LlmProvider = {
  provider: string
  display_name: string
  protocol: string
  default_base_url: string | null
  default_model?: string | null
  reasoning_efforts?: string[]
  supports_tool_choice: boolean
  default_max_tokens: number
}

/** Always available in the Models form, even if a stale engine omits it. */
export const REASONING_EFFORTS = ['none', 'low', 'medium', 'high', 'xhigh'] as const

export type ReasoningEffort = (typeof REASONING_EFFORTS)[number]

const PROVIDER_REASONING_EFFORTS: Record<string, readonly string[]> = {
  openai: REASONING_EFFORTS,
  'openai-response': REASONING_EFFORTS,
  azure: REASONING_EFFORTS,
  grok: REASONING_EFFORTS,
  openrouter: REASONING_EFFORTS,
  anthropic: REASONING_EFFORTS,
  gemini: ['none', 'low', 'medium', 'high'],
  deepseek: REASONING_EFFORTS,
  qwen: REASONING_EFFORTS,
}

export function reasoningEffortLabel(effort: string): string {
  if (effort === 'none') return 'None'
  if (effort === 'xhigh') return 'Extra high'
  if (effort === 'low') return 'Low'
  if (effort === 'medium') return 'Medium'
  if (effort === 'high') return 'High'
  return effort
}

export function reasoningEffortsFor(provider: string, fromApi?: string[]): string[] {
  if (fromApi && fromApi.length > 0) return fromApi
  return [...(PROVIDER_REASONING_EFFORTS[provider] ?? REASONING_EFFORTS)]
}

export const GROK_PROVIDER: LlmProvider = {
  provider: 'grok',
  display_name: 'Grok (xAI)',
  protocol: 'openai_compatible',
  default_base_url: 'https://api.x.ai/v1',
  default_model: 'grok-4.6',
  reasoning_efforts: ['none', 'low', 'medium', 'high', 'xhigh'],
  supports_tool_choice: true,
  default_max_tokens: 16384,
}

const FEATURED_PROVIDERS = ['anthropic', 'openai', 'grok', 'openai-response']

export function withKnownProviders(providers: LlmProvider[]): LlmProvider[] {
  const byId = new Map(providers.map((item) => [item.provider, item]))
  if (!byId.has(GROK_PROVIDER.provider)) {
    byId.set(GROK_PROVIDER.provider, GROK_PROVIDER)
  }
  return [...byId.values()].sort((left, right) => {
    const leftRank = FEATURED_PROVIDERS.indexOf(left.provider)
    const rightRank = FEATURED_PROVIDERS.indexOf(right.provider)
    if (leftRank === -1 && rightRank === -1) {
      return left.display_name.localeCompare(right.display_name)
    }
    if (leftRank === -1) return 1
    if (rightRank === -1) return -1
    return leftRank - rightRank
  })
}

export type LlmModel = {
  id: string
  provider: string
  model: string
  base_url: string | null
  label: string
  temperature: number | null
  api_key_masked: string
  max_tokens_per_day: number | null
  enabled: boolean
  supports_vision: boolean
  max_output_tokens: number | null
  request_timeout: number | null
  tenant_id: string | null
  is_default: boolean
  is_fallback: boolean
  is_secondary: boolean
  reasoning_effort?: string | null
  auth_kind?: string
  created_at: string
}

export type GrokSubscriptionStart = {
  session_id: string
  verification_url: string
  user_code: string
  expires_in: number
  interval: number
}

export type GrokSubscriptionStatus = {
  status: 'pending' | 'authorized' | 'expired' | 'denied' | 'error'
  session_id: string
  verification_url?: string | null
  user_code?: string | null
  model_id?: string | null
  detail?: string | null
  interval?: number | null
}

export type LlmModelWrite = {
  provider: string
  model: string
  api_key?: string
  base_url?: string | null
  label: string
  temperature?: number | null
  max_tokens_per_day?: number | null
  enabled: boolean
  supports_vision: boolean
  max_output_tokens?: number | null
  request_timeout?: number | null
  reasoning_effort?: string | null
}

export type LlmTestResult = {
  success: boolean
  latency_ms: number
  reply?: string
  error?: string
}

function tenantQuery(tenantId?: string): string {
  return tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
}

export async function listLlmProviders(): Promise<LlmProvider[]> {
  return apiRequest<LlmProvider[]>('/api/enterprise/llm-providers')
}

export async function listLlmModels(tenantId?: string): Promise<LlmModel[]> {
  return apiRequest<LlmModel[]>(`/api/enterprise/llm-models${tenantQuery(tenantId)}`)
}

export async function createLlmModel(input: LlmModelWrite, tenantId?: string): Promise<LlmModel> {
  return apiRequest<LlmModel>(`/api/enterprise/llm-models${tenantQuery(tenantId)}`, {
    method: 'POST',
    body: input,
  })
}

export async function updateLlmModel(modelId: string, input: Partial<LlmModelWrite>): Promise<LlmModel> {
  return apiRequest<LlmModel>(`/api/enterprise/llm-models/${modelId}`, {
    method: 'PUT',
    body: input,
  })
}

export async function setDefaultLlmModel(modelId: string): Promise<void> {
  return apiRequest(`/api/enterprise/llm-models/${modelId}/set-default`, { method: 'POST' })
}

export async function setFallbackLlmModel(modelId: string): Promise<void> {
  return apiRequest(`/api/enterprise/llm-models/${modelId}/set-fallback`, { method: 'POST' })
}

export async function setSecondaryLlmModel(modelId: string): Promise<void> {
  return apiRequest(`/api/enterprise/llm-models/${modelId}/set-secondary`, { method: 'POST' })
}

export async function deleteLlmModel(modelId: string, force = false): Promise<void> {
  const suffix = force ? '?force=true' : ''
  return apiRequest(`/api/enterprise/llm-models/${encodeURIComponent(modelId)}${suffix}`, {
    method: 'DELETE',
  })
}

export async function startGrokSubscription(tenantId?: string): Promise<GrokSubscriptionStart> {
  return apiRequest<GrokSubscriptionStart>(
    `/api/enterprise/llm-models/grok-subscription/start${tenantQuery(tenantId)}`,
    { method: 'POST' },
  )
}

export async function getGrokSubscriptionStatus(sessionId: string): Promise<GrokSubscriptionStatus> {
  const query = new URLSearchParams({ session_id: sessionId })
  return apiRequest<GrokSubscriptionStatus>(
    `/api/enterprise/llm-models/grok-subscription/status?${query.toString()}`,
  )
}

export async function testLlmModel(input: {
  provider: string
  model: string
  api_key?: string
  base_url?: string | null
  model_id?: string
}): Promise<LlmTestResult> {
  return apiRequest<LlmTestResult>('/api/enterprise/llm-test', {
    method: 'POST',
    body: input,
  })
}
