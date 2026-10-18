import type { QueryClient } from '@tanstack/react-query'

import { renameDepartment, type Department, type DepartmentsResponse } from '../lib/departments-api.ts'

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
