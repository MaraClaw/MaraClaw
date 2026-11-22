import { queryOptions } from '@tanstack/react-query'

import { groupDepartmentAssignments, listDepartmentAssignments } from '../lib/departments-api.ts'

export const departmentDirectoryKey = (tenantId: string) => ['admin-departments', tenantId] as const

export const departmentAssignmentsKey = (tenantId: string) => [...departmentDirectoryKey(tenantId), 'assignments'] as const

export function departmentAssignmentsOptions(tenantId: string, userIds: readonly string[]) {
  const sortedIds = [...new Set(userIds)].sort()
  return queryOptions({
    queryKey: [...departmentAssignmentsKey(tenantId), sortedIds] as const,
    queryFn: ({ signal }) => listDepartmentAssignments(tenantId, sortedIds, signal),
    select: groupDepartmentAssignments,
    enabled: sortedIds.length > 0,
  })
}
