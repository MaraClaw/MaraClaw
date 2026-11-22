import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'

import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import { listDepartments } from '@/lib/departments-api'
import { departmentAssignmentsOptions, departmentDirectoryKey } from './users-department-assignment-options'
import { createDepartmentOptions, deleteDepartmentOptions, mergeDepartmentOptions, replaceUserDepartmentsOptions, renameDepartmentOptions } from './users-department-mutations'

export function useMergeDepartment() {
  return useMutation(mergeDepartmentOptions(useQueryClient()))
}

export function useRenameDepartment() {
  return useMutation(renameDepartmentOptions(useQueryClient()))
}

export function useDeleteDepartment() {
  return useMutation(deleteDepartmentOptions(useQueryClient()))
}

export function useDepartments(tenantId: string, pageUserIds: readonly (readonly string[])[]) {
  const queryClient = useQueryClient()
  const departments = useQuery({
    queryKey: departmentDirectoryKey(tenantId),
    queryFn: ({ signal }) => listDepartments(tenantId, signal),
  })

  const assignments = useQueries({
    queries: pageUserIds.map((userIds) => departmentAssignmentsOptions(tenantId, userIds)),
  })

  const create = useMutation(createDepartmentOptions(queryClient))

  const assign = useMutation(replaceUserDepartmentsOptions(queryClient))

  return { departments, assignments, create, assign }
}
