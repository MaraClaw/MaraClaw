import { skipToken, useQuery } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { previewDepartmentMerge, type Department } from '@/lib/departments-api'
import { ApiError } from '@/lib/http'
import { useMergeDepartment } from './users-department-queries'

function isMergePreviewChanged(error: unknown) {
  return error instanceof ApiError && error.status === 409
    && typeof error.detail === 'object' && error.detail !== null
    && 'code' in error.detail && error.detail.code === 'department_merge_preview_changed'
}

export function DepartmentMerge({ department, departments, tenantId, onCancel, onMerged }: {
  readonly department: Department
  readonly departments: readonly Department[]
  readonly tenantId: string
  readonly onCancel: () => void
  readonly onMerged: () => void
}) {
  const { t, number } = useTranslation()
  const id = useId()
  const heading = useRef<HTMLHeadingElement>(null)
  const applying = useRef(false)
  const isCurrent = useSessionCurrent()
  const merge = useMergeDepartment()
  const [destinationId, setDestinationId] = useState('')
  const [confirmedToken, setConfirmedToken] = useState<string | null>(null)
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
  const confirmed = ready && confirmedToken === preview.data?.confirmation_token

  useEffect(() => { if (isCurrent()) heading.current?.focus() }, [isCurrent])
  useEffect(() => {
    if (merge.isError && isCurrent()) heading.current?.focus()
  }, [merge.isError, isCurrent])

  function apply() {
    if (applying.current || !confirmed || !ready || !preview.data || !isCurrent()) return
    applying.current = true
    merge.mutate({ ...selection, confirmationToken: preview.data.confirmation_token }, {
      onSuccess: (result) => {
        if (!isCurrent()) return
        onMerged()
        toast.success(t('Merged “{source}” into “{destination}”', { source: result.source.name, destination: result.destination.name }))
      },
      onError: (error) => {
        if (!isCurrent()) return
        if (isMergePreviewChanged(error)) {
          setConfirmedToken(null)
          setAttempt((value) => value + 1)
        }
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
      <h3 ref={heading} tabIndex={-1} id={`${id}-title`} className="text-sm font-semibold wrap-anywhere">{t('Merge “{name}”', { name: department.name })}</h3>
      <p id={`${id}-consequence`} className="text-sm text-muted-foreground">
        {t('Move memberships and eligible custom-agent use grants into an existing department, then permanently remove the source. Existing overlaps are kept only once. This cannot be undone.')}
      </p>
      <div className="grid min-w-0 gap-2">
        <Label htmlFor={`${id}-destination`}>{t('Destination department')}</Label>
        <Select id={`${id}-destination`} name="destination_id" value={destinationId} disabled={merge.isPending || destinations.length === 0}
          onChange={(event) => {
            setDestinationId(event.target.value)
            setConfirmedToken(null)
            setAttempt((value) => value + 1)
            merge.reset()
          }}>
          <option value="">{t('Select a department')}</option>
          {destinations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </Select>
      </div>
      {destinations.length === 0 ? <p className="text-sm text-muted-foreground">{t('Create another department before merging.')}</p> : null}
      <p role="status" className={preview.isFetching || preview.fetchStatus === 'paused' ? 'text-sm text-muted-foreground' : 'sr-only'}>
        {preview.fetchStatus === 'paused' ? t('Waiting for connection to load merge preview.') : preview.isFetching ? t('Loading merge preview…') : ''}
      </p>
      {preview.isError ? (
        <div className="space-y-2">
          <ErrorToast error={preview.error} message={preview.error instanceof ApiError ? preview.error.message : t('Could not load merge preview. Check your connection and try again.')} />
          <Button type="button" variant="outline" className="h-11" onClick={() => { if (!isCurrent()) return; heading.current?.focus(); setConfirmedToken(null); setAttempt((value) => value + 1) }}>{t('Retry preview')}</Button>
        </div>
      ) : null}
      {ready && preview.data ? (
        <div className="space-y-3 text-sm">
          <p className="font-medium wrap-anywhere">{t('“{source}” into “{destination}”', { source: preview.data.source.name, destination: preview.data.destination.name })}</p>
          <div className="space-y-1">
            <p className="font-medium">{t('Source description')}</p>
            <p className="whitespace-pre-wrap text-muted-foreground wrap-anywhere">{preview.data.source.description || t('No description')}</p>
          </div>
          <div className="space-y-1">
            <p className="font-medium">{t('Destination description (kept after merge)')}</p>
            <p className="whitespace-pre-wrap text-muted-foreground wrap-anywhere">{preview.data.destination.description || t('No description')}</p>
          </div>
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1">
              <dt>{t('Source memberships')}</dt><dd className="tabular-nums">{number(preview.data.source_membership_count)}</dd>
              <dt>{t('Destination memberships')}</dt><dd className="tabular-nums">{number(preview.data.destination_membership_count)}</dd>
              <dt>{t('Overlapping memberships')}</dt><dd className="tabular-nums">{number(preview.data.overlapping_membership_count)}</dd>
              <dt>{t('Source agent grants')}</dt><dd className="tabular-nums">{number(preview.data.source_agent_grant_count)}</dd>
              <dt>{t('Destination agent grants')}</dt><dd className="tabular-nums">{number(preview.data.destination_agent_grant_count)}</dd>
              <dt>{t('Overlapping agent grants')}</dt><dd className="tabular-nums">{number(preview.data.overlapping_agent_grant_count)}</dd>
          </dl>
          <p className="text-muted-foreground">{t('These are associations, not people losing access. Members joining the destination also gain its department-based agent access. Other memberships and access remain unchanged.')}</p>
          <p className="text-muted-foreground">{t('Your confirmation applies to this preview. If names, descriptions, memberships or eligible grants change, review a fresh preview and confirm again.')}</p>
          <label className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg py-2">
            <input type="checkbox" className="mt-1 size-5 shrink-0 accent-primary" checked={confirmed} disabled={merge.isPending} onChange={(event) => setConfirmedToken(event.target.checked ? preview.data.confirmation_token : null)} />
            <span className="min-w-0 wrap-anywhere">{t('I understand that “{source}” will be permanently merged into “{destination}”.', { source: preview.data.source.name, destination: preview.data.destination.name })}</span>
          </label>
        </div>
      ) : null}
      {merge.isError ? <ErrorToast error={merge.error} message={isMergePreviewChanged(merge.error) ? t('The merge preview changed. Review the refreshed preview and confirm again.') : merge.error instanceof ApiError ? merge.error.message : t('Could not merge departments. Check your connection and try again.')} /> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="ghost" className="h-11" disabled={merge.isPending} onClick={onCancel}>{t('Cancel')}</Button>
        <Button type="button" variant="destructive" className="h-11 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100" disabled={merge.isPending || !ready || !confirmed} onClick={apply}>{t('Merge departments')}</Button>
      </div>
      <p role="status" className={merge.isPending ? 'text-sm text-muted-foreground' : 'sr-only'}>{merge.isPending ? t('Merging departments…') : ''}</p>
    </fieldset>
  )
}
