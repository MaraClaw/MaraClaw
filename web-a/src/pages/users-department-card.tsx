import { zodResolver } from '@hookform/resolvers/zod'
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
import type { Department } from '@/lib/departments-api'
import { ApiError } from '@/lib/http'
import { useRenameDepartment } from './users-department-queries'

const schema = z.object({
  name: z.string().trim().min(1, 'Enter a department name').max(100, 'Use 100 characters or fewer'),
})
type FormValues = z.infer<typeof schema>

export function DepartmentCard({ department, tenantId }: {
  readonly department: Department
  readonly tenantId: string
}) {
  const formId = useId()
  const isCurrent = useSessionCurrent()
  const rename = useRenameDepartment()
  const [editing, setEditing] = useState(false)
  const editButton = useRef<HTMLButtonElement>(null)
  const wasEditing = useRef(false)
  const { register, handleSubmit, reset, setError, setFocus, clearErrors, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: department.name },
  })

  useEffect(() => {
    if (editing) setFocus('name')
    else if (wasEditing.current) editButton.current?.focus()
    wasEditing.current = editing
  }, [editing, setFocus])

  useEffect(() => {
    if (editing && !rename.isPending && (errors.name?.type === 'server' || errors.root?.server)) setFocus('name')
  }, [editing, rename.isPending, errors.name?.type, errors.root?.server, setFocus])

  function cancel() {
    reset({ name: department.name })
    rename.reset()
    setEditing(false)
  }

  function onSave(values: FormValues) {
    clearErrors()
    rename.mutate({ tenantId, departmentId: department.id, name: values.name }, {
      onSuccess: (updated) => {
        if (!isCurrent()) return
        setEditing(false)
        toast.success(`Renamed department to “${updated.name}”`)
      },
      onError: (error) => {
        if (!isCurrent()) return
        const duplicate = error instanceof ApiError && error.status === 409
        const message = duplicate
          ? 'A department with this name already exists. Use a different name.'
          : error instanceof ApiError ? error.message : 'Could not rename department. Check your connection and try again.'
        setError(duplicate ? 'name' : 'root.server', { type: 'server', message })
      },
    })
  }

  return (
    <Card className="min-w-0 gap-3 rounded-xl py-4 shadow-none">
      <CardContent className="px-4">
        {editing ? (
          <form
            className="space-y-3"
            aria-label={`Rename ${department.name}`}
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
            {errors.root?.server ? (
              <p id={`${formId}-error`} role="alert" className="text-sm text-destructive">{errors.root.server.message}</p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button type="submit" size="sm" className="h-11 sm:h-9" disabled={rename.isPending}>
                {rename.isPending ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : null}
                Save name
              </Button>
              <Button type="button" variant="ghost" size="sm" className="h-11 sm:h-9" disabled={rename.isPending} onClick={cancel}>
                Cancel
              </Button>
            </div>
            <p role="status" className={rename.isPending ? 'text-xs text-muted-foreground' : 'sr-only'}>
              {rename.isPending ? 'Saving department name…' : ''}
            </p>
          </form>
        ) : (
          <div className="flex items-baseline justify-between gap-3">
            <p className="min-w-0 text-sm font-medium leading-relaxed wrap-anywhere"><bdi>{department.name}</bdi></p>
            <Button
              ref={editButton}
              type="button"
              variant="ghost"
              size="sm"
              className="h-11 sm:h-9"
              onClick={() => {
                reset({ name: department.name })
                rename.reset()
                setEditing(true)
              }}
            >
              Edit name<span className="sr-only"> for {department.name}</span>
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
