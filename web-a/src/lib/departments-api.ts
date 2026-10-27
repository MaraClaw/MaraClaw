import { z } from 'zod'

import { apiRequest } from './http.ts'
import { DepartmentMergePreviewOutSchema } from './engine-contracts.generated.ts'

const departmentSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  name: z.string(),
  created_at: z.string(),
}).readonly()

const assignmentSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string(),
  department_id: z.string(),
})

const departmentsSchema = z.object({
  departments: z.array(departmentSchema).readonly(),
  assignments: z.array(assignmentSchema.readonly()).readonly(),
}).readonly()

const userDepartmentSchema = assignmentSchema.extend({
  department_id: z.string().nullable(),
}).readonly()

const userDepartmentsSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string(),
  department_ids: z.array(z.string()).readonly(),
}).readonly()

const deletionPreviewSchema = z.object({
  membership_count: z.number().int().nonnegative(),
  agent_grant_count: z.number().int().nonnegative(),
}).readonly()

export type DepartmentsResponse = z.infer<typeof departmentsSchema>
export type Department = z.infer<typeof departmentSchema>
export type UserDepartments = z.infer<typeof userDepartmentsSchema>
export type DepartmentMergePreview = z.infer<typeof DepartmentMergePreviewOutSchema>
export type DepartmentMergeSelection = {
  readonly tenantId: string
  readonly sourceId: string
  readonly destinationId: string
}
export type DepartmentMergeApply = DepartmentMergeSelection & {
  readonly confirmationToken: DepartmentMergePreview['confirmation_token']
}

export async function previewDepartmentMerge(selection: DepartmentMergeSelection, signal?: AbortSignal) {
  const query = new URLSearchParams({ tenant_id: selection.tenantId, destination_id: selection.destinationId })
  return apiRequest(`/api/departments/${encodeURIComponent(selection.sourceId)}/merge-preview?${query}`, {
    schema: DepartmentMergePreviewOutSchema, signal,
  })
}

export async function mergeDepartment(selection: DepartmentMergeApply) {
  const query = new URLSearchParams({ tenant_id: selection.tenantId })
  return apiRequest(`/api/departments/${encodeURIComponent(selection.sourceId)}/merge?${query}`, {
    schema: DepartmentMergePreviewOutSchema,
    method: 'POST',
    body: { destination_id: selection.destinationId, confirmation_token: selection.confirmationToken },
  })
}

export function groupDepartmentAssignments(assignments: DepartmentsResponse['assignments']): ReadonlyMap<string, readonly string[]> {
  const grouped = new Map<string, readonly string[]>()
  for (const assignment of assignments) {
    grouped.set(assignment.user_id, [...grouped.get(assignment.user_id) ?? [], assignment.department_id])
  }
  return grouped
}

export async function listDepartments(tenantId: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return apiRequest(`/api/departments?${query}`, { schema: departmentsSchema, signal })
}

export async function createDepartment(tenantId: string, input: { readonly name: string }) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return apiRequest(`/api/departments?${query}`, {
    schema: departmentSchema,
    method: 'POST',
    body: input,
  })
}

export async function renameDepartment(tenantId: string, departmentId: string, input: { readonly name: string }) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return apiRequest(`/api/departments/${encodeURIComponent(departmentId)}?${query}`, {
    schema: departmentSchema,
    method: 'PATCH',
    body: input,
  })
}

export async function deleteDepartment(tenantId: string, departmentId: string): Promise<void> {
  const query = new URLSearchParams({ tenant_id: tenantId })
  await apiRequest<void>(`/api/departments/${encodeURIComponent(departmentId)}?${query}`, { method: 'DELETE' })
}

export async function previewDepartmentDeletion(tenantId: string, departmentId: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return apiRequest(`/api/departments/${encodeURIComponent(departmentId)}/deletion-preview?${query}`, {
    schema: deletionPreviewSchema, signal,
  })
}

export async function setUserDepartment(
  tenantId: string,
  userId: string,
  departmentId: string | null,
) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return apiRequest(`/api/users/${encodeURIComponent(userId)}/department?${query}`, {
    schema: userDepartmentSchema,
    method: 'PUT',
    body: { department_id: departmentId },
  })
}

export async function setUserDepartments(tenantId: string, userId: string, departmentIds: readonly string[]) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return apiRequest(`/api/users/${encodeURIComponent(userId)}/departments?${query}`, {
    schema: userDepartmentsSchema,
    method: 'PUT',
    body: { department_ids: departmentIds },
  })
}
