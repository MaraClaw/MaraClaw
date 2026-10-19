import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import type { Department } from '@/lib/departments-api'
import { ApiError } from '@/lib/http'

export function DepartmentMemberships({
  name,
  departments,
  departmentIds,
  available,
  loading,
  pending,
  saving,
  error,
  onChange,
}: {
  readonly name: string
  readonly departments: readonly Department[]
  readonly departmentIds: readonly string[]
  readonly available: boolean
  readonly loading: boolean
  readonly pending: boolean
  readonly saving: boolean
  readonly error: Error | null
  readonly onChange: (departmentIds: readonly string[]) => void
}) {
  const id = useId()
  const description = `${id}-hint ${id}-status${error ? ` ${id}-error` : ''}`

  return (
    <fieldset
      className="relative z-20 grid w-full min-w-0 gap-1.5 sm:max-w-xs"
      disabled={!available || pending}
      aria-busy={saving}
      aria-describedby={description}
    >
      <legend className="mb-1.5 text-sm font-medium">
        Departments<span className="sr-only"> for {name}</span>
      </legend>
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        Select any departments. Changes save automatically.
      </p>
      {departments.length ? (
        <div className="grid max-h-48 gap-1.5 overflow-y-auto p-1">
          {departments.map((department) => (
            <Label
              key={department.id}
              className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border border-input bg-card px-3 py-2 has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-50"
            >
              <input
                type="checkbox"
                name="department_ids"
                value={department.id}
                checked={departmentIds.includes(department.id)}
                className="size-4 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                aria-invalid={error ? true : undefined}
                aria-describedby={description}
                onChange={(event) => onChange(event.target.checked
                  ? [...departmentIds, department.id]
                  : departmentIds.filter((departmentId) => departmentId !== department.id))}
              />
              <span className="min-w-0 leading-snug wrap-anywhere">{department.name}</span>
            </Label>
          ))}
        </div>
      ) : null}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-11 justify-self-start sm:h-9"
        disabled={departmentIds.length === 0}
        onClick={() => onChange([])}
      >
        Clear all<span className="sr-only"> departments for {name}</span>
      </Button>
      <p id={`${id}-status`} role="status" className="text-xs text-muted-foreground">
        {saving ? 'Saving departments…' : loading ? 'Loading departments…' : !available ? 'Departments unavailable'
          : departmentIds.length ? `${departmentIds.length} selected` : 'Unassigned'}
      </p>
      {error ? (
        <p id={`${id}-error`} className="text-xs text-destructive">
          {error instanceof ApiError ? error.message : 'Could not update departments. Try again.'}
        </p>
      ) : null}
    </fieldset>
  )
}
