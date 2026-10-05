import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createDepartment,
  listDepartments,
  setUserDepartment,
  type DepartmentsResponse,
} from '@/lib/departments-api'

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

  const assign = useMutation({
    mutationFn: (input: {
      readonly tenantId: string
      readonly userId: string
      readonly departmentId: string | null
    }) => setUserDepartment(input.tenantId, input.userId, input.departmentId),
    onSuccess: async (updated, input) => {
      const queryKey = ['admin-departments', input.tenantId]
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (current) => {
        if (!current) return undefined
        const assignments = current.assignments.filter((assignment) => assignment.user_id !== input.userId)
        return {
          ...current,
          assignments: updated.department_id === null
            ? assignments
            : [...assignments, {
                user_id: updated.user_id,
                tenant_id: updated.tenant_id,
                department_id: updated.department_id,
              }],
        }
      })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey, exact: true }),
        queryClient.invalidateQueries({ queryKey: ['admin-users', input.tenantId], exact: true }),
        queryClient.invalidateQueries({ queryKey: ['admin-user', input.userId], exact: true }),
      ])
    },
  })

  return { departments, create, assign }
}
