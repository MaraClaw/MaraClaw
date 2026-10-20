import { useId, useLayoutEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { listLocalDepartmentMembers, localDepartmentNameSchema, renameLocalDepartment, type LocalDepartment } from '@/lib/local-departments-api'

export function LocalDepartmentCard({ department, canEdit }: {
  readonly department: LocalDepartment
  readonly canEdit: boolean
}) {
  const scope = useWorkspaceSession()
  const inputId = useId()
  const rosterId = useId()
  const input = useRef<HTMLInputElement>(null)
  const editButton = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(department.name)
  const [fieldError, setFieldError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const roster = useQuery({
    queryKey: ['local-department-members', department.id],
    queryFn: ({ signal }) => listLocalDepartmentMembers(department.id, signal),
    enabled: expanded,
  })

  const rosterContent = (() => {
    switch (roster.status) {
      case 'pending':
        return <p role="status" className="text-base leading-normal text-muted-foreground sm:text-sm">Loading members...</p>
      case 'error':
        return <div className="space-y-3">
          <p role="status" className="text-base leading-normal text-destructive sm:text-sm">{userFacingRequestError(roster.error, 'Unable to load department members. Try again.')}</p>
          <Button type="button" variant="outline" disabled={roster.isFetching} onClick={scope.effect(() => { void roster.refetch() })}>Retry</Button>
        </div>
      case 'success':
        return roster.data.length === 0
          ? <p role="status" className="text-base leading-normal text-muted-foreground sm:text-sm">No members in this department yet.</p>
          : <ul className="space-y-2 text-lg leading-normal sm:text-sm" aria-label={`${department.name} member names`}>
            {roster.data.map((member) => <li key={member.membership_id} className="wrap-anywhere">{member.display_name?.trim() || 'Name unavailable'}</li>)}
          </ul>
      default:
        return roster satisfies never
    }
  })()

  function closeEditor() {
    restoreFocus.current = true
    setEditing(false)
  }

  const rename = useMutation({
    mutationFn: (nextName: Parameters<typeof renameLocalDepartment>[1]) => renameLocalDepartment(department.id, nextName),
    onSuccess: (updated) => {
      scope.client.setQueryData<readonly LocalDepartment[]>(['local-departments'], (current) =>
        current?.map((item) => item.id === updated.id ? { id: updated.id, name: updated.name } : item),
      )
      closeEditor()
      void Promise.all([
        scope.client.invalidateQueries({ queryKey: ['local-departments'] }),
        scope.client.invalidateQueries({ queryKey: ['permissions'] }),
        scope.client.invalidateQueries({ queryKey: ['permission-departments'] }),
      ])
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setFieldError('A department with this name already exists. Choose another name.')
        input.current?.focus()
      } else {
        setSaveError(userFacingRequestError(error, 'Unable to save the department name. Try again.'))
      }
    },
  })

  useLayoutEffect(() => {
    if (!scope.isCurrent()) return
    if (editing && !rename.isPending) {
      input.current?.focus()
      input.current?.select()
    } else if (!editing && !rename.isPending && restoreFocus.current) {
      editButton.current?.focus()
      restoreFocus.current = false
    }
  }, [editing, rename.isPending, scope])

  return (
    <Card className="min-w-0">
      <CardContent className="space-y-4">
        {editing ? (
          <form
            aria-label={`Rename ${department.name}`}
            aria-busy={rename.isPending}
            className="space-y-3"
            noValidate
            onSubmit={(event) => {
              event.preventDefault()
              if (!scope.isCurrent()) return
              if (rename.isPending || !canEdit) return
              const parsed = localDepartmentNameSchema.safeParse(name)
              setSaveError('')
              if (!parsed.success) {
                setFieldError(parsed.error.issues[0]?.message ?? 'Enter a department name.')
                input.current?.focus()
                return
              }
              setFieldError('')
              rename.mutate(parsed.data)
            }}
            onKeyDown={(event) => {
              if (!scope.isCurrent()) return
              if (event.key === 'Escape' && !rename.isPending) {
                event.preventDefault()
                closeEditor()
              }
            }}
          >
            <div className="space-y-2">
              <Label htmlFor={inputId}>Department name</Label>
              <Input
                ref={input}
                id={inputId}
                name="departmentName"
                autoComplete="off"
                value={name}
                readOnly={rename.isPending}
                aria-invalid={Boolean(fieldError)}
                aria-describedby={fieldError ? `${inputId}-error` : undefined}
                onChange={scope.effect((event) => {
                  setName(event.target.value)
                  setFieldError('')
                  setSaveError('')
                })}
              />
              {fieldError ? <p id={`${inputId}-error`} role="status" className="text-sm text-destructive">{fieldError}</p> : null}
            </div>
            {saveError ? <p role="status" className="text-sm text-destructive">{saveError}</p> : null}
            <div className="flex flex-wrap gap-3">
              <Button type="submit" disabled={rename.isPending}>{rename.isPending ? 'Saving...' : 'Save'}</Button>
              <Button type="button" variant="outline" disabled={rename.isPending} onClick={() => {
                if (scope.isCurrent()) closeEditor()
              }}>Cancel</Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-wrap items-start justify-between gap-3">
            <h3 className="min-w-0 flex-1 wrap-anywhere font-display text-base font-semibold leading-snug">{department.name}</h3>
            {canEdit ? (
              <Button
                ref={editButton}
                variant="outline"
                aria-label={`Edit name for ${department.name}`}
                disabled={rename.isPending}
                onClick={scope.effect(() => {
                  setName(department.name)
                  setFieldError('')
                  setSaveError('')
                  rename.reset()
                  setEditing(true)
                })}
              >Edit name</Button>
            ) : null}
          </div>
        )}
        <Button
          type="button"
          variant="outline"
          aria-label={`${expanded ? 'Hide' : 'Show'} members for ${department.name}`}
          aria-expanded={expanded}
          aria-controls={rosterId}
          onClick={scope.effect(() => { setExpanded((current) => !current) })}
        >{expanded ? 'Hide members' : 'Show members'}</Button>
        <section id={rosterId} aria-label={`${department.name} members`} hidden={!expanded}>
          {expanded ? rosterContent : null}
        </section>
      </CardContent>
    </Card>
  )
}
