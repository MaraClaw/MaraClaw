import { zodResolver } from '@hookform/resolvers/zod'
import { skipToken, useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useSessionCurrent } from '@/hooks/use-auth'
import { previewDepartmentDeletion, type Department } from '@/lib/departments-api'
import { ApiError } from '@/lib/http'
import { useDeleteDepartment, useRenameDepartment } from './users-department-queries'
import { DepartmentMerge } from './users-department-merge'

const schema = z.object({
  name: z.string().trim().min(1, 'Enter a department name').max(100, 'Use 100 characters or fewer'),
  description: z.string(),
})
type FormValues = z.infer<typeof schema>

export function DepartmentCard({ department, departments, tenantId, onDeleted }: {
  readonly department: Department
  readonly departments: readonly Department[]
  readonly tenantId: string
  readonly onDeleted: () => void
}) {
  const formId = useId()
  const isCurrent = useSessionCurrent()
  const rename = useRenameDepartment()
  const deletion = useDeleteDepartment()
  const [editing, setEditing] = useState(false)
  const [merging, setMerging] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [previewAttempt, setPreviewAttempt] = useState(0)
  const preview = useQuery({
    queryKey: ['admin-department-deletion-preview', tenantId, department.id, formId, previewAttempt, confirming],
    queryFn: confirming ? ({ signal }) => previewDepartmentDeletion(tenantId, department.id, signal) : skipToken,
    gcTime: 0,
    retry: false,
  })
  const previewReady = preview.isSuccess && !preview.isFetching
  const editButton = useRef<HTMLButtonElement>(null)
  const mergeButton = useRef<HTMLButtonElement>(null)
  const wasMerging = useRef(false)
  const deleteButton = useRef<HTMLButtonElement>(null)
  const confirmHeading = useRef<HTMLHeadingElement>(null)
  const deleting = useRef(false)
  const wasConfirming = useRef(false)
  const wasEditing = useRef(false)
  const { register, handleSubmit, reset, setError, setFocus, clearErrors, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: department.name, description: department.description },
  })

  useEffect(() => {
    if (editing) setFocus('name')
    else if (wasEditing.current) editButton.current?.focus()
    wasEditing.current = editing
  }, [editing, setFocus])

  useEffect(() => {
    if (!merging && wasMerging.current) mergeButton.current?.focus()
    wasMerging.current = merging
  }, [merging])

  useEffect(() => {
    if (confirming) confirmHeading.current?.focus()
    else if (wasConfirming.current) deleteButton.current?.focus()
    wasConfirming.current = confirming
  }, [confirming])

  useEffect(() => {
    if (editing && !rename.isPending && (errors.name?.type === 'server' || errors.root?.server)) setFocus('name')
  }, [editing, rename.isPending, errors.name?.type, errors.root?.server, setFocus])

  function cancel() {
    reset({ name: department.name, description: department.description })
    rename.reset()
    setEditing(false)
  }

  function onSave(values: FormValues) {
    clearErrors()
    rename.mutate({ tenantId, departmentId: department.id, name: values.name,
      ...(values.description === department.description.replace(/\r\n?/g, '\n') ? {} : { description: values.description }),
    }, {
      onSuccess: (updated) => {
        if (!isCurrent()) return
        setEditing(false)
        toast.success(`Updated department “${updated.name}”`)
      },
      onError: (error) => {
        if (!isCurrent()) return
        const duplicate = error instanceof ApiError && error.status === 409
        const message = duplicate
          ? 'A department with this name already exists. Use a different name.'
          : error instanceof ApiError ? error.message : 'Could not update department. Check your connection and try again.'
        setError(duplicate ? 'name' : 'root.server', { type: 'server', message })
      },
    })
  }

  function cancelDeletion() {
    deletion.reset()
    setConfirming(false)
  }

  function onDelete() {
    if (deleting.current || !previewReady || !isCurrent()) return
    deleting.current = true
    deletion.mutate({ tenantId, departmentId: department.id }, {
      onSuccess: () => {
        if (!isCurrent()) return
        onDeleted()
        toast.success(`Deleted department “${department.name}”`)
      },
      onSettled: () => { deleting.current = false },
    })
  }

  return (
    <Card className="min-w-0 gap-3 rounded-xl py-4 shadow-none">
      <CardContent className="px-4">
        {merging ? <DepartmentMerge department={department} departments={departments} tenantId={tenantId} onCancel={() => setMerging(false)} onMerged={onDeleted} /> : confirming ? (
          <fieldset
            aria-labelledby={`${formId}-delete-title`}
            aria-describedby={`${formId}-delete-consequence`}
            aria-busy={deletion.isPending}
            className="space-y-3"
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !event.nativeEvent.isComposing && !deletion.isPending) {
                event.preventDefault()
                event.stopPropagation()
                cancelDeletion()
              }
            }}
          >
            <h3 ref={confirmHeading} tabIndex={-1} id={`${formId}-delete-title`} className="text-sm font-semibold wrap-anywhere">
              Delete “{department.name}”?
            </h3>
            <p id={`${formId}-delete-consequence`} className="text-sm text-muted-foreground">
              This permanently removes the department, its member assignments and its department-based agent access grants. Members and their other access stay unchanged. This cannot be undone.
            </p>
            {preview.isFetching ? <p role="status" className="text-sm text-muted-foreground">Loading deletion preview…</p> : preview.isError ? (
              <div className="space-y-2">
                <p role="alert" className="text-sm text-destructive">Could not load deletion preview. Try again before deleting.</p>
                <Button type="button" variant="outline" size="sm" className="h-11 sm:h-9" onClick={() => { confirmHeading.current?.focus(); void preview.refetch() }}>Retry preview</Button>
              </div>
            ) : preview.data ? (
              <div className="space-y-2 text-sm">
                <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1">
                  <dt>Department memberships</dt><dd className="tabular-nums">{preview.data.membership_count}</dd>
                  <dt>Department-to-agent grants</dt><dd className="tabular-nums">{preview.data.agent_grant_count}</dd>
                </dl>
                <p className="text-xs text-muted-foreground sm:text-sm">These associations will be removed, not people. Other access may remain. Counts may change before deletion.</p>
              </div>
            ) : null}
            {deletion.isError ? (
              <p role="alert" className="text-sm text-destructive wrap-anywhere">
                {deletion.error instanceof ApiError ? deletion.error.message : 'Could not delete department. Check your connection and try again.'}
              </p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="ghost" size="sm" className="h-11 sm:h-9" disabled={deletion.isPending} onClick={cancelDeletion}>
                Cancel
              </Button>
              <Button type="button" variant="destructive" size="sm" className="h-11 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100 sm:h-9" disabled={deletion.isPending || !previewReady} onClick={onDelete}>
                Delete department
              </Button>
            </div>
            <p role="status" className={deletion.isPending ? 'text-xs text-muted-foreground' : 'sr-only'}>
              {deletion.isPending ? 'Deleting department…' : ''}
            </p>
          </fieldset>
        ) : editing ? (
          <form
            className="space-y-3"
            aria-label={`Edit ${department.name}`}
            aria-busy={rename.isPending}
            aria-describedby={errors.root?.server ? `${formId}-error` : undefined}
            onSubmit={handleSubmit(onSave)}
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !event.nativeEvent.isComposing && !rename.isPending) {
                event.preventDefault()
                event.stopPropagation()
                cancel()
              }
            }}
            noValidate
          >
            <div className="grid min-w-0 gap-1.5">
              <Label htmlFor={`${formId}-name`}>Department name</Label>
              <Input
                id={`${formId}-name`}
                autoComplete="off"
                required
                maxLength={100}
                disabled={rename.isPending}
                aria-invalid={errors.name ? true : undefined}
                aria-describedby={errors.name ? `${formId}-name-error` : `${formId}-hint`}
                {...register('name')}
              />
              <p id={`${formId}-hint`} className="text-xs text-muted-foreground">Up to 100 characters.</p>
              {errors.name ? (
                <p id={`${formId}-name-error`} className="text-xs text-destructive">{errors.name.message}</p>
              ) : null}
            </div>
            <div className="grid min-w-0 gap-1.5">
              <Label htmlFor={`${formId}-description`}>Description (optional)</Label>
              <textarea id={`${formId}-description`} rows={3} disabled={rename.isPending}
                className="w-full min-w-0 resize-y rounded-xl border border-input bg-card px-3.5 py-2 text-base text-foreground shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 disabled:opacity-50 sm:text-sm"
                {...register('description')} />
            </div>
            {errors.root?.server ? (
              <p id={`${formId}-error`} role="alert" className="text-sm text-destructive">{errors.root.server.message}</p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button type="submit" size="sm" className="h-11 sm:h-9" disabled={rename.isPending}>
                {rename.isPending ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : null}
                Save department
              </Button>
              <Button type="button" variant="ghost" size="sm" className="h-11 sm:h-9" disabled={rename.isPending} onClick={cancel}>
                Cancel
              </Button>
            </div>
            <p role="status" className={rename.isPending ? 'text-xs text-muted-foreground' : 'sr-only'}>
              {rename.isPending ? 'Saving department…' : ''}
            </p>
          </form>
        ) : (
          <div className="space-y-2">
            <p className="min-w-0 text-sm font-medium leading-relaxed wrap-anywhere"><bdi>{department.name}</bdi></p>
            <p className="whitespace-pre-wrap text-sm text-muted-foreground wrap-anywhere">{department.description || 'No description'}</p>
            <div className="flex flex-wrap gap-1">
            <Button
              ref={editButton}
              type="button"
              variant="ghost"
              size="sm"
              className="h-11 sm:h-9"
              onClick={() => {
                reset({ name: department.name, description: department.description })
                rename.reset()
                setEditing(true)
              }}
            >
              Edit department<span className="sr-only"> for {department.name}</span>
            </Button>
            <Button ref={mergeButton} type="button" variant="ghost" size="sm" className="h-11 sm:h-9" onClick={() => setMerging(true)}>
              Merge<span className="sr-only"> {department.name}</span>
            </Button>
            <Button
              ref={deleteButton}
              type="button"
              variant="ghost"
              size="sm"
              className="h-11 text-destructive sm:h-9"
              onClick={() => { deletion.reset(); setPreviewAttempt((value) => value + 1); setConfirming(true) }}
            >
              Delete<span className="sr-only"> {department.name}</span>
            </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
