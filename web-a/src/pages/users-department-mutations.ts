import type { QueryClient } from '@tanstack/react-query'

import { renameDepartment, setUserDepartments, type Department, type DepartmentsResponse, type UserDepartments } from '../lib/departments-api.ts'

type RenameDepartmentInput = {
  readonly tenantId: string
  readonly departmentId: string
  readonly name: string
}

export function renameDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: RenameDepartmentInput) => renameDepartment(input.tenantId, input.departmentId, { name: input.name }),
    onSuccess: (updated: Department, input: RenameDepartmentInput) => {
      const queryKey = ['admin-departments', input.tenantId]
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (current) => current
        ? {
            ...current,
            departments: current.departments.map((department) => department.id === updated.id ? updated : department),
          }
        : undefined)
      void queryClient.invalidateQueries({ queryKey, exact: true })
    },
  }
}

type ReplaceUserDepartmentsInput = {
  readonly tenantId: string
  readonly userId: string
  readonly departmentIds: readonly string[]
}

export function replaceUserDepartmentsOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: ReplaceUserDepartmentsInput) => setUserDepartments(input.tenantId, input.userId, input.departmentIds),
    onSuccess: async (updated: UserDepartments, input: ReplaceUserDepartmentsInput) => {
      const queryKey = ['admin-departments', input.tenantId]
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (current) => current
        ? {
            ...current,
            assignments: [
              ...current.assignments.filter((assignment) => assignment.user_id !== input.userId),
              ...updated.department_ids.map((department_id) => ({
                user_id: updated.user_id,
                tenant_id: updated.tenant_id,
                department_id,
              })),
            ],
          }
        : undefined)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey, exact: true }),
        queryClient.invalidateQueries({ queryKey: ['admin-users', input.tenantId], exact: true }),
        queryClient.invalidateQueries({ queryKey: ['admin-user', input.userId], exact: true }),
      ])
    },
  }
}
