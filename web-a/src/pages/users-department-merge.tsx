import { skipToken, useQuery } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useSessionCurrent } from '@/hooks/use-auth'
import { previewDepartmentMerge, type Department } from '@/lib/departments-api'
import { ApiError } from '@/lib/http'
import { useMergeDepartment } from './users-department-queries'

export function DepartmentMerge({ department, departments, tenantId, onCancel, onMerged }: {
  readonly department: Department
  readonly departments: readonly Department[]
  readonly tenantId: string
  readonly onCancel: () => void
  readonly onMerged: () => void
}) {
  const id = useId()
  const heading = useRef<HTMLHeadingElement>(null)
  const applying = useRef(false)
  const isCurrent = useSessionCurrent()
  const merge = useMergeDepartment()
  const [destinationId, setDestinationId] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const destinations = departments.filter((item) => item.id !== department.id && item.tenant_id === tenantId)
  const destination = destinations.find((item) => item.id === destinationId)
  const selection = { tenantId, sourceId: department.id, destinationId }
  const preview = useQuery({
    queryKey: ['admin-department-merge-preview', tenantId, department.id, destinationId, id, attempt],
    queryFn: destination ? ({ signal }) => previewDepartmentMerge(selection, signal) : skipToken,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const ready = !!destination && preview.isSuccess && !preview.isFetching

  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => {
    if (merge.isError) heading.current?.focus()
  }, [merge.isError])

  function apply() {
    if (applying.current || !confirmed || !ready || !isCurrent()) return
    applying.current = true
    merge.mutate(selection, {
      onSuccess: (result) => {
        if (!isCurrent()) return
        onMerged()
        toast.success(`Merged “${result.source.name}” into “${result.destination.name}”`)
      },
      onSettled: () => { applying.current = false },
    })
  }

  return (
    <fieldset
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-consequence`}
      aria-busy={merge.isPending}
      className="min-w-0 space-y-3"
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !event.nativeEvent.isComposing && !merge.isPending) {
          event.preventDefault()
          event.stopPropagation()
          onCancel()
        }
      }}
    >
      <h3 ref={heading} tabIndex={-1} id={`${id}-title`} className="text-sm font-semibold wrap-anywhere">Merge “{department.name}”</h3>
      <p id={`${id}-consequence`} className="text-sm text-muted-foreground">
        Move memberships and eligible custom-agent use grants into an existing department, then permanently remove the source. Existing overlaps are kept only once. This cannot be undone.
      </p>
      <div className="grid min-w-0 gap-2">
        <Label htmlFor={`${id}-destination`}>Destination department</Label>
        <Select id={`${id}-destination`} name="destination_id" value={destinationId} disabled={merge.isPending || destinations.length === 0}
          onChange={(event) => {
            setDestinationId(event.target.value)
            setConfirmed(false)
            setAttempt((value) => value + 1)
            merge.reset()
          }}>
          <option value="">Select a department</option>
          {destinations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </Select>
      </div>
      {destinations.length === 0 ? <p className="text-sm text-muted-foreground">Create another department before merging.</p> : null}
      <p role="status" className={preview.isFetching ? 'text-sm text-muted-foreground' : 'sr-only'}>{preview.isFetching ? 'Loading merge preview…' : ''}</p>
      {preview.isError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive wrap-anywhere">{preview.error instanceof ApiError ? preview.error.message : 'Could not load merge preview. Check your connection and try again.'}</p>
          <Button type="button" variant="outline" className="h-11" onClick={() => { heading.current?.focus(); setConfirmed(false); void preview.refetch() }}>Retry preview</Button>
        </div>
      ) : null}
      {ready && preview.data ? (
        <div className="space-y-3 text-sm">
          <p className="font-medium wrap-anywhere">“{preview.data.source.name}” into “{preview.data.destination.name}”</p>
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1">
            <dt>Source memberships</dt><dd className="tabular-nums">{preview.data.source_membership_count}</dd>
            <dt>Destination memberships</dt><dd className="tabular-nums">{preview.data.destination_membership_count}</dd>
            <dt>Overlapping memberships</dt><dd className="tabular-nums">{preview.data.overlapping_membership_count}</dd>
            <dt>Source agent grants</dt><dd className="tabular-nums">{preview.data.source_agent_grant_count}</dd>
            <dt>Destination agent grants</dt><dd className="tabular-nums">{preview.data.destination_agent_grant_count}</dd>
            <dt>Overlapping agent grants</dt><dd className="tabular-nums">{preview.data.overlapping_agent_grant_count}</dd>
          </dl>
          <p className="text-muted-foreground">These are associations, not people losing access. Members joining the destination also gain its department-based agent access. Other memberships and access remain unchanged.</p>
          <p className="text-muted-foreground">Preview only: counts may change. Applying the merge rechecks current state and combines everything in one transaction.</p>
          <label className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg py-2">
            <input type="checkbox" className="mt-1 size-5 shrink-0 accent-primary" checked={confirmed} disabled={merge.isPending} onChange={(event) => setConfirmed(event.target.checked)} />
            <span className="min-w-0 wrap-anywhere">I understand that “{preview.data.source.name}” will be permanently merged into “{preview.data.destination.name}”.</span>
          </label>
        </div>
      ) : null}
      {merge.isError ? <p role="alert" className="text-sm text-destructive wrap-anywhere">{merge.error instanceof ApiError ? merge.error.message : 'Could not merge departments. Check your connection and try again.'}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="ghost" className="h-11" disabled={merge.isPending} onClick={onCancel}>Cancel</Button>
        <Button type="button" variant="destructive" className="h-11 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100" disabled={merge.isPending || !ready || !confirmed} onClick={apply}>Merge departments</Button>
      </div>
      <p role="status" className={merge.isPending ? 'text-sm text-muted-foreground' : 'sr-only'}>{merge.isPending ? 'Merging departments…' : ''}</p>
    </fieldset>
  )
}
