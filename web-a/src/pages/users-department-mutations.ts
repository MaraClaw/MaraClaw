import type { QueryClient } from '@tanstack/react-query'

import { deleteDepartment, mergeDepartment, renameDepartment, setUserDepartments, type Department, type DepartmentMergeApply, type DepartmentMergePreview, type DepartmentsResponse, type UserDepartments } from '../lib/departments-api.ts'

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

type DeleteDepartmentInput = {
  readonly tenantId: string
  readonly departmentId: string
}

export function deleteDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: DeleteDepartmentInput) => deleteDepartment(input.tenantId, input.departmentId),
    onSuccess: (_deleted: void, input: DeleteDepartmentInput) => {
      const queryKey = ['admin-departments', input.tenantId]
      const current = queryClient.getQueryData<DepartmentsResponse>(queryKey)
      const affectedUsers = new Set(current?.assignments
        .filter((assignment) => assignment.department_id === input.departmentId)
        .map((assignment) => assignment.user_id))
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (directory) => directory
        ? {
            ...directory,
            departments: directory.departments.filter((department) => department.id !== input.departmentId),
            assignments: directory.assignments.filter((assignment) => assignment.department_id !== input.departmentId),
          }
        : undefined)
      void queryClient.invalidateQueries({ queryKey, exact: true })
      void queryClient.invalidateQueries({ queryKey: ['admin-users', input.tenantId] })
      for (const userId of affectedUsers) {
        void queryClient.invalidateQueries({ queryKey: ['admin-user', userId], exact: true })
      }
    },
  }
}

export function mergeDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: DepartmentMergeApply) => mergeDepartment(input),
    retry: false,
    onSuccess: (merged: DepartmentMergePreview, input: DepartmentMergeApply) => {
      const queryKey = ['admin-departments', input.tenantId]
      const current = queryClient.getQueryData<DepartmentsResponse>(queryKey)
      const affectedUsers = new Set(current?.assignments
        .filter((assignment) => assignment.department_id === input.sourceId || assignment.department_id === input.destinationId)
        .map((assignment) => assignment.user_id))
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (directory) => {
        if (!directory) return undefined
        const destinations = new Set(directory.assignments
          .filter((assignment) => assignment.department_id === input.destinationId)
          .map((assignment) => assignment.user_id))
        return {
          departments: directory.departments.filter((department) => department.id !== input.sourceId)
            .map((department) => department.id === input.destinationId ? merged.destination : department),
          assignments: directory.assignments.flatMap((assignment) => assignment.department_id !== input.sourceId
            ? [assignment]
            : destinations.has(assignment.user_id) ? [] : [{ ...assignment, department_id: input.destinationId }]),
        }
      })
      void queryClient.invalidateQueries({ queryKey, exact: true })
      void queryClient.invalidateQueries({ queryKey: ['admin-users', input.tenantId] })
      for (const userId of affectedUsers) {
        void queryClient.invalidateQueries({ queryKey: ['admin-user', userId], exact: true })
      }
    },
  }
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
