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

export const orgDepartmentNameSchema = z.string().trim()
  .min(1, 'Enter a department name.')
  .max(100, 'Use 100 characters or fewer.')
  .brand<'OrgDepartmentName'>()

export type OrgDepartment = Readonly<z.infer<typeof departmentSchema>>
export type OrgDepartmentMember = z.infer<typeof departmentMemberSchema>
type OrgDepartmentName = z.infer<typeof orgDepartmentNameSchema>

export async function listOrgDepartments(signal: AbortSignal): Promise<readonly OrgDepartment[]> {
  const response = await apiRequest('/api/departments/options', { schema: optionsSchema, signal })
  return response.departments
}

export async function listOrgDepartmentMembers(departmentId: string, signal: AbortSignal): Promise<readonly OrgDepartmentMember[]> {
  const response = await apiRequest(`/api/departments/${encodeURIComponent(departmentId)}/members`, { schema: membersSchema, signal })
  return response.members
}

export async function renameOrgDepartment(departmentId: string, name: OrgDepartmentName) {
  return apiRequest(`/api/departments/${encodeURIComponent(departmentId)}`, {
    schema: renamedDepartmentSchema, method: 'PATCH', body: { name },
  })
}
