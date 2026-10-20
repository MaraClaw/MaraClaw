import { z } from 'zod'

import { apiRequest } from './http.ts'

const departmentSchema = z.object({ id: z.string(), name: z.string() })
const optionsSchema = z.object({ departments: z.array(departmentSchema.readonly()).readonly() })
const departmentMemberSchema = z.object({
  membership_id: z.uuid(),
  display_name: z.string().nullable(),
}).readonly()
const membersSchema = z.object({ members: z.array(departmentMemberSchema).readonly() })
const renamedDepartmentSchema = departmentSchema.extend({
  tenant_id: z.string(),
  created_at: z.string(),
}).readonly()

export const localDepartmentNameSchema = z.string().trim()
  .min(1, 'Enter a department name.')
  .max(100, 'Use 100 characters or fewer.')
  .brand<'LocalDepartmentName'>()

export type LocalDepartment = Readonly<z.infer<typeof departmentSchema>>
export type LocalDepartmentMember = z.infer<typeof departmentMemberSchema>
type LocalDepartmentName = z.infer<typeof localDepartmentNameSchema>

export async function listLocalDepartments(signal: AbortSignal): Promise<readonly LocalDepartment[]> {
  const response = await apiRequest<unknown>('/api/departments/options', { signal })
  return optionsSchema.parse(response).departments
}

export async function listLocalDepartmentMembers(departmentId: string, signal: AbortSignal): Promise<readonly LocalDepartmentMember[]> {
  const response = await apiRequest<unknown>(`/api/departments/${encodeURIComponent(departmentId)}/members`, { signal })
  return membersSchema.parse(response).members
}

export async function renameLocalDepartment(departmentId: string, name: LocalDepartmentName) {
  const response = await apiRequest<unknown>(`/api/departments/${encodeURIComponent(departmentId)}`, {
    method: 'PATCH', body: { name },
  })
  return renamedDepartmentSchema.parse(response)
}
