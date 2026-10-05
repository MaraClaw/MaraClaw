import { z } from 'zod'

import { apiRequest } from '@/lib/http'

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

export type DepartmentsResponse = z.infer<typeof departmentsSchema>

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
