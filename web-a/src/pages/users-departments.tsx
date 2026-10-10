import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useEffect, useId } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { UserCard } from '@/components/users/user-card'
import { useSessionCurrent } from '@/hooks/use-auth'
import { fenceCallback } from '@/hooks/session-mutation'
import { ApiError } from '@/lib/http'
import { isEndUserRole, type AdminUser } from '@/lib/users-api'
import { useDepartments } from '@/pages/users-department-queries'

const schema = z.object({
  name: z.string().trim().min(1, 'Enter a department name').max(100, 'Use 100 characters or fewer'),
})

type FormValues = z.infer<typeof schema>

export function TenantUsers({
  tenantId,
  rows,
  canToggle,
  pending,
  onToggle,
}: {
  readonly tenantId: string
  readonly rows: readonly AdminUser[]
  readonly canToggle: (row: AdminUser) => boolean
  readonly pending: boolean
  readonly onToggle: (row: AdminUser) => void
}) {
  const formId = useId()
  const isCurrent = useSessionCurrent()
  const { departments, create, assign } = useDepartments(tenantId)
  const {
    register,
    handleSubmit,
    reset,
    setError,
    setFocus,
    clearErrors,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: '' },
  })
  const assignments = new Map((departments.data?.assignments ?? []).map((assignment) =>
    [assignment.user_id, assignment.department_id] as const,
  ))
  const savingAssignment = assign.isPending ? assign.variables : undefined

  useEffect(() => {
    if (!create.isPending && errors.name?.type === 'server') {
      setFocus('name')
    }
  }, [create.isPending, errors.name?.type, setFocus])

  function onCreate(values: FormValues) {
    clearErrors()
    create.mutate({ tenantId, name: values.name }, {
      onSuccess: (created) => {
        if (!isCurrent()) return
        reset()
        toast.success(`Created department “${created.name}”`)
      },
      onError: (error) => {
        if (!isCurrent()) return
        const duplicate = error instanceof ApiError && error.status === 409
        const message = duplicate
          ? 'A department with this name already exists. Use a different name.'
          : error instanceof ApiError ? error.message : 'Could not create department. Check your connection and try again.'
        setError(duplicate ? 'name' : 'root.server', { type: 'server', message })
        toast.error(message)
      },
    })
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Departments</CardTitle>
          <CardDescription>
            Create local departments for this company, then assign members below. Each person can belong to one department.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form
            className="space-y-2"
            onSubmit={handleSubmit(onCreate)}
            noValidate
            aria-busy={create.isPending}
            aria-describedby={errors.root?.server ? `${formId}-error` : undefined}
          >
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
              <div className="grid min-w-0 flex-1 gap-1.5">
                <Label htmlFor={`${formId}-name`}>Department name</Label>
                <Input
                  id={`${formId}-name`}
                  autoComplete="off"
                  required
                  maxLength={100}
                  disabled={create.isPending}
                  aria-invalid={errors.name ? true : undefined}
                  aria-describedby={errors.name ? `${formId}-name-error` : `${formId}-name-hint`}
                  {...register('name')}
                />
              </div>
              <Button type="submit" disabled={create.isPending}>
                {create.isPending ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : null}
                Create department
              </Button>
            </div>
            <p id={`${formId}-name-hint`} className="text-xs text-muted-foreground">
              Up to 100 characters.
            </p>
            {errors.name ? (
              <p id={`${formId}-name-error`} className="text-xs text-destructive">{errors.name.message}</p>
            ) : null}
            {errors.root?.server ? (
              <p id={`${formId}-error`} role="alert" className="text-sm text-destructive">
                {errors.root.server.message}
              </p>
            ) : null}
            <p role="status" className={create.isPending ? 'text-xs text-muted-foreground' : 'sr-only'}>
              {create.isPending ? 'Creating department…' : ''}
            </p>
          </form>
          {departments.isLoading ? <p className="text-sm text-muted-foreground" role="status">Loading departments…</p> : null}
          {departments.error ? (
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-sm text-destructive" role="alert">
                {departments.error instanceof ApiError ? departments.error.message : 'Failed to load departments'}
              </p>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={departments.isFetching}
                onClick={() => void departments.refetch()}
              >
                Retry
              </Button>
            </div>
          ) : null}
          {departments.isSuccess && departments.data.departments.length === 0 ? (
            <p className="text-sm text-muted-foreground">No departments yet. Create one to start grouping members.</p>
          ) : null}
        </CardContent>
      </Card>
      <div className="grid gap-4">
        {rows.map((row) => {
          const selectorId = `${formId}-${row.id}-department`
          const isSaving = savingAssignment?.userId === row.id
          const departmentId = isSaving ? savingAssignment.departmentId : assignments.get(row.id) ?? null
          const error = assign.variables?.userId === row.id ? assign.error : null
          const name = row.display_name || row.email || row.username || 'user'
          return (
            <UserCard
              key={row.id}
              row={row}
              canToggle={canToggle(row)}
              pending={pending}
              onToggle={() => onToggle(row)}
            >
              {isEndUserRole(row.role) && row.tenant_id === tenantId ? (
                <div className="relative z-20 grid w-full min-w-0 gap-1.5 sm:max-w-xs">
                  <Label htmlFor={selectorId}>
                    Department<span className="sr-only"> for {name}</span>
                  </Label>
                  <Select
                    id={selectorId}
                    value={departmentId ?? ''}
                    disabled={!departments.isSuccess || assign.isPending}
                    aria-busy={isSaving}
                    aria-invalid={error ? true : undefined}
                    aria-describedby={error ? `${selectorId}-error` : `${selectorId}-status`}
                    onChange={(event) => {
                      const nextDepartmentId = event.target.value || null
                      if (nextDepartmentId === departmentId) return
                      assign.mutate({ tenantId, userId: row.id, departmentId: nextDepartmentId }, {
                        onSuccess: fenceCallback(isCurrent, () => toast.success(`Department updated for ${name}`)),
                        onError: fenceCallback(isCurrent, (saveError) => toast.error(saveError instanceof ApiError ? saveError.message : 'Could not update department. Try again.')),
                      })
                    }}
                  >
                    <option value="">
                      {departments.data ? 'Unassigned' : departments.isLoading ? 'Loading departments…' : 'Departments unavailable'}
                    </option>
                    {(departments.data?.departments ?? []).map((department) => (
                      <option key={department.id} value={department.id}>{department.name}</option>
                    ))}
                  </Select>
                  <p
                    id={`${selectorId}-status`}
                    role="status"
                    className={isSaving ? 'text-xs text-muted-foreground' : 'sr-only'}
                  >
                    {isSaving ? 'Saving department…' : ''}
                  </p>
                  {error ? (
                    <p id={`${selectorId}-error`} className="text-xs text-destructive">
                      {error instanceof ApiError ? error.message : 'Could not update department. Try again.'}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </UserCard>
          )
        })}
      </div>
    </>
  )
}
