import { apiRequest } from './http.ts'
import { z } from 'zod'
import { FocusItemResponseSchema, TaskOutSchema, TriggerResponseSchema } from './engine-contracts.generated.ts'
import type { FocusUpsertBody, TaskCreate } from './engine-contracts.generated.ts'
export type { TaskOut, FocusItemResponse as FocusItem } from './engine-contracts.generated.ts'

export async function listTasks(agentId: string) {
  return apiRequest(`/api/agents/${agentId}/tasks/`, { schema: z.array(TaskOutSchema) })
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
