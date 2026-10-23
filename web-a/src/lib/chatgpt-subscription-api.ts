import { z } from 'zod'

import { ApiError, apiRequest } from './http.ts'
import { StaleSessionError } from './session-policy.ts'

const startSchema = z.object({
  attempt_id: z.string().min(1),
  authorization_url: z.url({ protocol: /^https$/u }),
  redirect_uri: z.url({ protocol: /^https?$/u }),
  expires_in: z.number().nonnegative(),
}).readonly()
const statusSchema = z.object({
  status: z.enum(['pending', 'authorized', 'expired', 'denied', 'error']),
  attempt_id: z.string().min(1),
  model_id: z.string().nullable().optional(),
  detail: z.string().nullable().optional(),
  expires_in: z.number().nonnegative().nullable().optional(),
}).readonly()

export type ChatGPTSubscriptionStart = z.infer<typeof startSchema>
export type ChatGPTSubscriptionStatus = z.infer<typeof statusSchema>
export type ChatGPTSubscriptionComplete = {
  readonly attempt_id: string
  readonly callback_url: string
}

// Never retain a server/transport error that may echo the pasted address.
export class ChatGPTSubscriptionError extends Error {
  readonly status: number | null

  constructor(status: number | null) {
    super('ChatGPT sign-in request did not complete.')
    this.name = 'ChatGPTSubscriptionError'
    this.status = status
  }
}

async function request<T>(schema: z.ZodType<T>, response: Promise<unknown>): Promise<T> {
  try {
    return schema.parse(await response)
  } catch (error) {
    if (error instanceof StaleSessionError) throw error
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ChatGPTSubscriptionError(error instanceof ApiError ? error.status : null)
  }
}

export function startChatGPTSubscription(tenantId?: string): Promise<ChatGPTSubscriptionStart> {
  const scope = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return request(startSchema, apiRequest<unknown>(`/api/enterprise/llm-models/chatgpt-subscription/start${scope}`, { method: 'POST' }))
}

export function completeChatGPTSubscription(input: ChatGPTSubscriptionComplete): Promise<ChatGPTSubscriptionStatus> {
  return request(statusSchema, apiRequest<unknown>('/api/enterprise/llm-models/chatgpt-subscription/complete', {
    method: 'POST',
    body: { attempt_id: input.attempt_id, callback_url: input.callback_url },
  }))
}

export function getChatGPTSubscriptionStatus(attemptId: string, signal?: AbortSignal): Promise<ChatGPTSubscriptionStatus> {
  const query = new URLSearchParams({ attempt_id: attemptId })
  return request(statusSchema, apiRequest<unknown>(`/api/enterprise/llm-models/chatgpt-subscription/status?${query}`, { signal }))
}
