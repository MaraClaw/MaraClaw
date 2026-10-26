import { useQuery, useQueryClient } from '@tanstack/react-query'

import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import {
  createDepartment,
  listDepartments,
  type DepartmentsResponse,
} from '@/lib/departments-api'
import { deleteDepartmentOptions, mergeDepartmentOptions, replaceUserDepartmentsOptions, renameDepartmentOptions } from './users-department-mutations'

export function useMergeDepartment() {
  return useMutation(mergeDepartmentOptions(useQueryClient()))
}

export function useRenameDepartment() {
  return useMutation(renameDepartmentOptions(useQueryClient()))
}

export function useDeleteDepartment() {
  return useMutation(deleteDepartmentOptions(useQueryClient()))
}

export function useDepartments(tenantId: string) {
  const queryClient = useQueryClient()
  const departments = useQuery({
    queryKey: ['admin-departments', tenantId],
    queryFn: ({ signal }) => listDepartments(tenantId, signal),
  })

  const create = useMutation({
    mutationFn: (input: { readonly tenantId: string; readonly name: string }) =>
      createDepartment(input.tenantId, { name: input.name }),
    onSuccess: async (created, input) => {
      const queryKey = ['admin-departments', input.tenantId]
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (current) => current
        ? {
            ...current,
            departments: [...current.departments.filter((department) => department.id !== created.id), created],
          }
        : undefined)
      await queryClient.invalidateQueries({ queryKey, exact: true })
    },
  })

  const assign = useMutation(replaceUserDepartmentsOptions(queryClient))

  return { departments, create, assign }
}
