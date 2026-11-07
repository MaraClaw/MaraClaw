import { apiRequest } from './http.ts'
import { z } from 'zod'
import { FocusItemResponseSchema, TaskLogPageSchema, TaskOutSchema, TaskPageSchema, TriggerResponseSchema } from './engine-contracts.generated.ts'
import type { FocusUpsertBody, TaskCreate, TaskStatus, TaskType } from './engine-contracts.generated.ts'
import { authSession, type SessionOwner } from './auth-session.ts'
export type { TaskOut, FocusItemResponse as FocusItem } from './engine-contracts.generated.ts'

export async function listTasks(agentId: string) {
  return apiRequest(`/api/agents/${agentId}/tasks/`, { schema: z.array(TaskOutSchema) })
}

export type TaskTarget = { readonly agentId: string; readonly taskId: string }
type PageRequest = {
  readonly cursor?: string | null
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export async function listTaskPage(agentId: string, request: PageRequest & {
  readonly status?: TaskStatus
  readonly type?: TaskType
  readonly limit?: number
} = {}) {
  const params = new URLSearchParams({ limit: String(request.limit ?? 50) })
  if (request.cursor != null) params.set('cursor', request.cursor)
  if (request.status !== undefined) params.set('status_filter', request.status)
  if (request.type !== undefined) params.set('type_filter', request.type)
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/tasks/page?${params}`, {
    schema: TaskPageSchema, owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}

export async function getTask(target: TaskTarget, request: Pick<PageRequest, 'owner' | 'signal'> = {}) {
  return apiRequest(`/api/agents/${encodeURIComponent(target.agentId)}/tasks/${encodeURIComponent(target.taskId)}`, {
    schema: TaskOutSchema, owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}

export async function listTaskLogPage(target: TaskTarget, request: PageRequest = {}) {
  const params = new URLSearchParams({ limit: '50' })
  if (request.cursor != null) params.set('cursor', request.cursor)
  return apiRequest(`/api/agents/${encodeURIComponent(target.agentId)}/tasks/${encodeURIComponent(target.taskId)}/logs/page?${params}`, {
    schema: TaskLogPageSchema, owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}

export async function createTask(agentId: string, body: TaskCreate) {
  return apiRequest(`/api/agents/${agentId}/tasks/`, { schema: TaskOutSchema, method: 'POST', body })
}

export async function triggerTask(agentId: string, taskId: string): Promise<unknown> {
  return apiRequest(`/api/agents/${agentId}/tasks/${taskId}/trigger`, { method: 'POST' })
}

export async function listFocus(agentId: string) {
  return apiRequest(`/api/agents/${agentId}/focus/`, { schema: z.array(FocusItemResponseSchema) })
}

export async function upsertFocus(agentId: string, body: FocusUpsertBody) {
  return apiRequest(`/api/agents/${agentId}/focus/`, { schema: FocusItemResponseSchema, method: 'POST', body })
}

export async function completeFocus(agentId: string, key: string) {
  return apiRequest(`/api/agents/${agentId}/focus/${encodeURIComponent(key)}/complete`, { schema: FocusItemResponseSchema, method: 'POST' })
}

export async function listTriggers(agentId: string) {
  return apiRequest(`/api/agents/${agentId}/triggers`, { schema: z.array(TriggerResponseSchema) })
}

export async function patchTrigger(agentId: string, triggerId: string, body: { is_enabled?: boolean }): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/triggers/${triggerId}`, { method: 'PATCH', body })
}

export async function deleteTrigger(agentId: string, triggerId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/triggers/${triggerId}`, { method: 'DELETE' })
}

export async function listTaskLogs(agentId: string, taskId: string): Promise<Array<{ id?: string; content: string; created_at?: string }>> {
  return apiRequest(`/api/agents/${agentId}/tasks/${taskId}/logs`)
}
