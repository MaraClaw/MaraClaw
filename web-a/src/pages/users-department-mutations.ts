import type { QueryClient, QueryFilters } from '@tanstack/react-query'

import { createDepartment, deleteDepartment, mergeDepartment, renameDepartment, setUserDepartments, type Department, type DepartmentAssignments, type DepartmentInput, type DepartmentMergeApply, type DepartmentMergePreview, type DepartmentsResponse, type UserDepartments } from '../lib/departments-api.ts'

import { departmentAssignmentsKey, departmentDirectoryKey } from './users-department-assignment-options.ts'

type CreateDepartmentInput = DepartmentInput & { readonly tenantId: string }

export function createDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: ({ tenantId, ...input }: CreateDepartmentInput) => createDepartment(tenantId, input),
    onSuccess: async (created: Department, input: CreateDepartmentInput) => {
      const queryKey = departmentDirectoryKey(input.tenantId)
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (current) => current
        ? { ...current, departments: [...current.departments.filter((department) => department.id !== created.id), created] }
        : undefined)
      await queryClient.invalidateQueries({ queryKey, exact: true })
    },
  }
}

type RenameDepartmentInput = DepartmentInput & {
  readonly tenantId: string
  readonly departmentId: string
}

export function renameDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: ({ tenantId, departmentId, ...input }: RenameDepartmentInput) => renameDepartment(tenantId, departmentId, input),
    onSuccess: (updated: Department, input: RenameDepartmentInput) => {
      const queryKey = departmentDirectoryKey(input.tenantId)
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

function refreshDepartmentMemberships(queryClient: QueryClient, tenantId: string) {
  void queryClient.invalidateQueries({ queryKey: departmentDirectoryKey(tenantId) })
  void queryClient.invalidateQueries({ queryKey: ['admin-users', tenantId] })
  void queryClient.invalidateQueries({ queryKey: ['admin-user'] })
}

export function deleteDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: DeleteDepartmentInput) => deleteDepartment(input.tenantId, input.departmentId),
    onSuccess: (_deleted: void, input: DeleteDepartmentInput) => {
      const queryKey = departmentDirectoryKey(input.tenantId)
      void queryClient.cancelQueries({ queryKey })
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (directory) => directory
        ? { departments: directory.departments.filter((department) => department.id !== input.departmentId) }
        : undefined)
      refreshDepartmentMemberships(queryClient, input.tenantId)
    },
  }
}

export function mergeDepartmentOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: DepartmentMergeApply) => mergeDepartment(input),
    retry: false,
    onSuccess: (merged: DepartmentMergePreview, input: DepartmentMergeApply) => {
      const queryKey = departmentDirectoryKey(input.tenantId)
      void queryClient.cancelQueries({ queryKey })
      queryClient.setQueryData<DepartmentsResponse>(queryKey, (directory) => directory
        ? {
            departments: directory.departments.filter((department) => department.id !== input.sourceId)
              .map((department) => department.id === input.destinationId ? merged.destination : department),
          }
        : undefined)
      refreshDepartmentMemberships(queryClient, input.tenantId)
    },
  }
}

export function replaceUserDepartmentsOptions(queryClient: QueryClient) {
  return {
    mutationFn: (input: ReplaceUserDepartmentsInput) => setUserDepartments(input.tenantId, input.userId, input.departmentIds),
    onSuccess: (updated: UserDepartments, input: ReplaceUserDepartmentsInput) => {
      const filters = {
        queryKey: departmentAssignmentsKey(input.tenantId),
        predicate: ({ queryKey: [, , , userIds] }) => Array.isArray(userIds) && userIds.includes(input.userId),
      } satisfies QueryFilters
      // Preserve any reconciliation already required for other rows on these pages.
      const refresh = new Set(queryClient.getQueryCache().findAll(filters)
        .filter(({ state }) => state.isInvalidated || state.fetchStatus !== 'idle')
        .map(({ queryHash }) => queryHash))
      void queryClient.cancelQueries(filters)
      const replacement = updated.department_ids.map((department_id) => ({
        user_id: updated.user_id,
        tenant_id: updated.tenant_id,
        department_id,
      }))
      queryClient.setQueriesData<DepartmentAssignments>(filters, (current) => current
        ? [...current.filter((row) => row.user_id !== input.userId), ...replacement]
        : undefined)
      void queryClient.invalidateQueries({ ...filters, predicate: ({ queryHash }) => refresh.has(queryHash) })
      // The detail DTO does not carry department IDs; refresh just this user's entry.
      return queryClient.invalidateQueries({ queryKey: ['admin-user', input.userId], exact: true })
    },
  }
}
