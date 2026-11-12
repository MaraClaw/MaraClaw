import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { useTranslation } from '@/hooks/use-translation'
import type { Department } from '@/lib/departments-api'

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
  const { t, number } = useTranslation()
  const id = useId()
  const description = `${id}-hint ${id}-status`

  return (
    <fieldset
      className="relative z-20 grid w-full min-w-0 gap-1.5 sm:max-w-xs"
      disabled={!available || pending}
      aria-busy={saving}
      aria-describedby={description}
    >
      <legend className="mb-1.5 text-sm font-medium">
        {t('Departments')}<span className="sr-only"> {t('for {name}', { name })}</span>
      </legend>
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {t('Select any departments. Changes save automatically.')}
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
        {t('Clear all')}<span className="sr-only"> {t('departments for {name}', { name })}</span>
      </Button>
      <p id={`${id}-status`} role="status" className="text-xs text-muted-foreground">
        {saving ? t('Saving departments…') : loading ? t('Loading departments…') : !available ? t('Departments unavailable')
          : departmentIds.length ? t('{count} selected', { count: number(departmentIds.length) }) : t('Unassigned')}
      </p>
    </fieldset>
  )
}
