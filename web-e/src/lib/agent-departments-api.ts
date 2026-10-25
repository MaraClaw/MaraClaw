import { z } from 'zod'

import { apiRequest } from '@/lib/http'

const departmentSchema = z.object({ id: z.string(), name: z.string() }).readonly()
const departmentsResponseSchema = z.object({ departments: z.array(departmentSchema).readonly() }).readonly()

export type PermissionDepartment = z.infer<typeof departmentSchema>

export async function listAgentPermissionDepartments(
  agentId: string,
  signal: AbortSignal,
): Promise<readonly PermissionDepartment[]> {
  const response = await apiRequest(`/api/agents/${agentId}/permissions/departments`, { schema: departmentsResponseSchema, signal })
  return response.departments
}
