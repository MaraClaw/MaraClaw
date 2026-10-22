import { z } from 'zod'

import { apiRequest } from './http.ts'

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

export type DepartmentsResponse = z.infer<typeof departmentsSchema>
export type Department = z.infer<typeof departmentSchema>
export type UserDepartments = z.infer<typeof userDepartmentsSchema>

export function groupDepartmentAssignments(assignments: DepartmentsResponse['assignments']): ReadonlyMap<string, readonly string[]> {
  const grouped = new Map<string, readonly string[]>()
  for (const assignment of assignments) {
    grouped.set(assignment.user_id, [...grouped.get(assignment.user_id) ?? [], assignment.department_id])
  }
  return grouped
}

export async function listDepartments(tenantId: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return departmentsSchema.parse(
    await apiRequest<unknown>(`/api/departments?${query}`, { signal }),
  )
}

export async function createDepartment(tenantId: string, input: { readonly name: string }) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return departmentSchema.parse(
    await apiRequest<unknown>(`/api/departments?${query}`, {
      method: 'POST',
      body: input,
    }),
  )
}

export async function renameDepartment(tenantId: string, departmentId: string, input: { readonly name: string }) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return departmentSchema.parse(
    await apiRequest<unknown>(`/api/departments/${encodeURIComponent(departmentId)}?${query}`, {
      method: 'PATCH',
      body: input,
    }),
  )
}

export async function deleteDepartment(tenantId: string, departmentId: string): Promise<void> {
  const query = new URLSearchParams({ tenant_id: tenantId })
  await apiRequest<void>(`/api/departments/${encodeURIComponent(departmentId)}?${query}`, { method: 'DELETE' })
}

export async function setUserDepartment(
  tenantId: string,
  userId: string,
  departmentId: string | null,
) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return userDepartmentSchema.parse(
    await apiRequest<unknown>(`/api/users/${encodeURIComponent(userId)}/department?${query}`, {
      method: 'PUT',
      body: { department_id: departmentId },
    }),
  )
}

export async function setUserDepartments(tenantId: string, userId: string, departmentIds: readonly string[]) {
  const query = new URLSearchParams({ tenant_id: tenantId })
  return userDepartmentsSchema.parse(
    await apiRequest<unknown>(`/api/users/${encodeURIComponent(userId)}/departments?${query}`, {
      method: 'PUT',
      body: { department_ids: departmentIds },
    }),
  )
}
