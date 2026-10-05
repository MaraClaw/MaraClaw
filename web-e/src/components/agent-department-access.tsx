import { useQuery } from '@tanstack/react-query'
import { useLayoutEffect, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { listAgentPermissionDepartments } from '@/lib/agent-departments-api'
import type { AgentPermissions } from '@/lib/workspace-api'

type AgentDepartmentAccessProps = {
  readonly agentId: string
  readonly departmentIds: readonly string[]
  readonly departmentAccess: AgentPermissions['department_access']
  readonly isCustom: boolean
  readonly disabled: boolean
  readonly onChange: (departmentIds: readonly string[]) => void
}

export function AgentDepartmentAccess({
  agentId,
  departmentIds,
  departmentAccess,
  isCustom,
  disabled,
  onChange,
}: AgentDepartmentAccessProps) {
  const [departmentId, setDepartmentId] = useState('')
  const fieldsetRef = useRef<HTMLFieldSetElement>(null)
  const selectRef = useRef<HTMLSelectElement>(null)
  const focusAfterChange = useRef<readonly string[] | null>(null)
  const departments = useQuery({
    queryKey: ['permission-departments', agentId],
    queryFn: ({ signal }) => listAgentPermissionDepartments(agentId, signal),
  })
  const available = (departments.data ?? []).filter((department) => !departmentIds.includes(department.id))
  const chosen = available.find((department) => department.id === departmentId)

  useLayoutEffect(() => {
    if (focusAfterChange.current !== departmentIds) return
    focusAfterChange.current = null
    if (selectRef.current?.disabled) fieldsetRef.current?.focus()
    else selectRef.current?.focus()
  }, [departmentIds])

  if (!isCustom) {
    return (
      <p className="text-sm text-muted-foreground">
        Department sharing is available in Custom list. Saving Private or Whole company clears department grants.
      </p>
    )
  }

  return (
    <fieldset ref={fieldsetRef} tabIndex={-1} className="min-w-0 space-y-3">
      <legend className="text-sm font-semibold">Departments</legend>
      <p id="agent-departments-description" className="text-sm text-muted-foreground">
        Company admins manage these local departments separately from the provider directory. Members can use this
        agent, not manage it, and membership changes apply automatically. Only selected departments are included, not
        their parent or child departments.
      </p>
      <div className="space-y-2">
        <Label htmlFor="agent-department">Add a department</Label>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Select
            ref={selectRef}
            id="agent-department"
            value={chosen?.id ?? ''}
            onChange={(event) => setDepartmentId(event.target.value)}
            disabled={disabled || departments.isPending || departments.isError || available.length === 0}
            aria-describedby="agent-departments-description agent-departments-status"
            className="min-w-0 flex-1"
          >
            <option value="">Choose a department</option>
            {available.map((department) => (
              <option key={department.id} value={department.id}>
                {department.name}
              </option>
            ))}
          </Select>
          <Button
            type="button"
            variant="outline"
            disabled={disabled || departments.isError || !chosen}
            onClick={() => {
              if (!chosen) return
              const nextIds = [...departmentIds, chosen.id]
              focusAfterChange.current = nextIds
              onChange(nextIds)
              setDepartmentId('')
            }}
          >
            Add department
          </Button>
        </div>
        <p id="agent-departments-status" role="status" className="text-sm text-muted-foreground">
          {departments.isPending
            ? 'Loading departments…'
            : departments.isError
              ? 'Unable to load departments. Your selections are unchanged.'
              : departments.data?.length === 0
                ? 'No local departments yet. Ask a company admin to create one.'
                : available.length === 0
                  ? 'All available departments are selected.'
                  : ''}
        </p>
        {departments.isError ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={departments.isFetching}
            onClick={() => void departments.refetch()}
          >
            Retry departments
          </Button>
        ) : null}
      </div>
      {departmentIds.length > 0 ? (
        <ul className="space-y-2" aria-label="Selected departments">
          {departmentIds.map((id) => {
            const name = departmentAccess.find((department) => department.id === id)?.name
              ?? departments.data?.find((department) => department.id === id)?.name
              ?? id
            return (
              <li key={id} className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2 text-sm">
                <span className="min-w-0 flex-1 break-words">{name}</span>
                <div className="flex shrink-0 items-center gap-2">
                  <Badge variant="soft">Use only</Badge>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    aria-label={`Remove ${name}`}
                    disabled={disabled}
                    onClick={() => {
                      const nextIds = departmentIds.filter((selectedId) => selectedId !== id)
                      focusAfterChange.current = nextIds
                      onChange(nextIds)
                    }}
                  >
                    Remove
                  </Button>
                </div>
              </li>
            )
          })}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">No departments selected.</p>
      )}
    </fieldset>
  )
}
