import { useTranslation } from '@/hooks/use-translation'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
import { AddKeyResult } from '@/components/okr-reports'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useMutation, useQuery } from '@/hooks/session-workspace'
import { usePageNavigation } from '@/hooks/use-page-navigation'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { createObjective, listObjectivePage, updateKrProgress, type OkrPeriod } from '@/lib/okr-api'

export function OkrObjectives({ period, isAdmin }: { readonly period: OkrPeriod; readonly isAdmin: boolean }) {
  const { t: translate, owned, formatNumber } = useTranslation()
  const prefix = ['okr', 'objectives', 'page', period.start, period.end]
  const navigation = usePageNavigation(prefix)
  const [title, setTitle] = useState('')
  const objectives = useQuery({
    queryKey: [...prefix, navigation.revision, navigation.cursor],
    queryFn: ({ signal }) => listObjectivePage({
      periodStart: period.start, periodEnd: period.end, cursor: navigation.cursor, signal,
    }),
    retry: false,
  })
  const create = useMutation({
    mutationFn: (title: string) => createObjective({
      title, owner_type: 'company', period_start: period.start, period_end: period.end,
    }),
    onSuccess() {
      void navigation.reset()
      if (!navigation.isCurrent()) return
      setTitle('')
      toast.success(translate("Objective created"))
    },
    onError(error) {
      if (navigation.isCurrent()) toast.error(userFacingRequestError(error, translate("Could not create objective")))
    },
  })
  const progress = useMutation({
    mutationFn: ({ id, value }: { readonly id: string; readonly value: number }) => updateKrProgress(id, value),
    onSuccess() { void navigation.reset() },
    onError(error) {
      if (navigation.isCurrent()) toast.error(userFacingRequestError(error, translate("Could not update progress")))
    },
  })
  const cursorRejected = objectives.error instanceof ApiError && objectives.error.status === 400
  return (
    <>
      {objectives.isLoading ? <Loader2 aria-label={translate("Loading objectives")} className="size-4 animate-spin text-muted-foreground" /> : null}
      {objectives.isError ? (
        <div className="space-y-2">
          <RequestErrorToast error={objectives.error} message={userFacingRequestError(objectives.error, translate("Unable to load objectives."))} />
          <Button size="sm" variant="outline" onClick={() => cursorRejected ? navigation.reset() : objectives.refetch()}>
            {cursorRejected ? translate("Restart objectives") : translate("Retry objectives")}
          </Button>
        </div>
      ) : null}
      {objectives.data?.items.length === 0 ? <p className="text-sm text-muted-foreground">{translate("No objectives in this period.")}</p> : null}
      <ul className="space-y-3" aria-label={translate("Objectives")}>
        {(objectives.data?.items ?? []).map((objective) => (
          <li key={objective.id} className="rounded-2xl border border-border p-4">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium">{objective.title}</p>
                <p className="text-xs text-muted-foreground">{objective.owner_name ?? owned(objective.owner_type)} · {owned(objective.status)}</p>
              </div>
              <Badge variant="soft">{objective.owner_type}</Badge>
            </div>
            {objective.description ? <p className="mt-2 text-sm text-muted-foreground">{objective.description}</p> : null}
            <ul className="mt-3 space-y-2">
              {(objective.key_results ?? []).map((kr) => {
                const ratio = kr.target_value ? Math.min(1, kr.current_value / kr.target_value) : 0
                return (
                  <li key={kr.id}>
                    <div className="flex justify-between text-xs">
                      <span>{kr.title}</span>
                    <span>{formatNumber(kr.current_value)}/{formatNumber(kr.target_value)}{kr.unit ? ` ${kr.unit}` : ''}</span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className="h-full bg-primary" style={{ width: `${ratio * 100}%` }} />
                    </div>
                    {isAdmin ? (
                      <button type="button" className="mt-1 text-xs text-primary" disabled={progress.isPending} onClick={() => {
                        if (!navigation.isCurrent()) return
                      const next = window.prompt(translate('New current value'), String(kr.current_value))
                        if (next) progress.mutate({ id: kr.id, value: Number(next) })
                      }}>{translate("Update progress")}</button>
                    ) : null}
                  </li>
                )
              })}
            </ul>
            {isAdmin ? <AddKeyResult objectiveId={objective.id} onAdded={() => {
              void navigation.reset()
            }} /> : null}
          </li>
        ))}
      </ul>
      <nav aria-label={translate("Objective pages")} className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={navigation.page === 1 || objectives.isFetching} onClick={navigation.previous}>{translate("Previous")}</Button>
        <span role="status" className="text-sm text-muted-foreground">{translate("Page")}{' '}{navigation.page}</span>
        <Button size="sm" variant="outline" disabled={!objectives.data?.next_cursor || objectives.isFetching} onClick={() => {
          if (objectives.data?.next_cursor) navigation.next(objectives.data.next_cursor)
        }}>{translate("Next")}</Button>
      </nav>
      {isAdmin ? (
        <form className="space-y-2 rounded-2xl border border-border p-4" onSubmit={(event) => {
          event.preventDefault()
          if (navigation.isCurrent() && title.trim()) create.mutate(title.trim())
        }}>
          <Label htmlFor="obj-title">{translate("New company objective")}</Label>
          <div className="flex gap-2">
            <Input id="obj-title" value={title} onChange={(event) => setTitle(event.target.value)} />
            <Button type="submit" size="sm" disabled={create.isPending || !title.trim()}>{translate("Add")}</Button>
          </div>
        </form>
      ) : null}
    </>
  )
}
