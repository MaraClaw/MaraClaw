import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useEffect, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { ScheduleRunHistory } from './schedule-run-history'
import { ScheduleRunReceipt } from './schedule-run-receipt'
import {
  createSchedule,
  deleteSchedule,
  listSchedules,
  runSchedule,
  updateSchedule,
  type ScheduleOut,
} from '@/lib/schedules-api'
import type { AgentOut } from '@/lib/workspace-api'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { emptyScheduleDraft, scheduleChanges, type ScheduleDraft } from '@/lib/schedule-editor'
import { formatScheduleNextRun, scheduleTimezoneError } from '@/lib/schedule-time'
import { useAuth } from '@/hooks/use-auth'

export function AgentSchedulesPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  return <AgentSchedules key={agent.id} agent={agent} />
}

function AgentSchedules({ agent }: { readonly agent: AgentOut }) {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const isCreator = user?.id === agent.creator_id
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(emptyScheduleDraft)
  const [editing, setEditing] = useState<ScheduleOut | null>(null)
  const [timezoneError, setTimezoneError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const timezoneInput = useRef<HTMLInputElement>(null)
  const [historyId, setHistoryId] = useState<string | null>(null)
  const runIntents = useRef(new Map<string, string>())
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const [runMessages, setRunMessages] = useState<Readonly<Record<string, string>>>({})
  const [acceptedRun, setAcceptedRun] = useState<Readonly<{ scheduleId: string; runId: string }> | null>(null)
  const schedules = useQuery({ queryKey: ['schedules', agent.id], queryFn: () => listSchedules(agent.id) })

  const save = useMutation({
    mutationFn: (values: ScheduleDraft) => editing
      ? updateSchedule(agent.id, editing.id, scheduleChanges(editing, values))
      : createSchedule(agent.id, { ...values, is_enabled: true }),
    onSuccess() {
      setEditing(null)
      setDraft(emptyScheduleDraft)
      void queryClient.invalidateQueries({ queryKey: ['schedules', agent.id] })
    },
    onError(error) {
      setSaveError(userFacingRequestError(error, 'Unable to save schedule. Check the cron and IANA timezone, then try again. If timezone data is unavailable, ask your admin to update it.'))
    },
  })
  const run = useMutation({
    mutationFn: ({ id, key }: { readonly id: string; readonly key: string }) => runSchedule(agent.id, id, key),
    onSuccess(accepted, { id }) {
      if (!mounted.current) return
      runIntents.current.delete(id)
      setRunMessages((messages) => ({ ...messages, [id]: 'Run accepted. Check history for completion.' }))
      setAcceptedRun({ scheduleId: id, runId: accepted.run_id })
    },
    onError(error, { id }) {
      if (!mounted.current) return
      const definite = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408
      if (definite) runIntents.current.delete(id)
      setRunMessages((messages) => ({ ...messages, [id]: definite
        ? userFacingRequestError(error, 'Unable to run schedule. Try again.')
        : 'Acceptance could not be confirmed. Retry Run to check the same request without starting another run.' }))
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteSchedule(agent.id, id),
    onSuccess() { void queryClient.invalidateQueries({ queryKey: ['schedules', agent.id] }) },
    onError(error) { toast.error(userFacingRequestError(error, 'Unable to delete schedule. Try again.')) },
  })

  function requestRun(id: string) {
    if (run.isPending) return
    const key = runIntents.current.get(id) ?? crypto.randomUUID()
    runIntents.current.set(id, key)
    setRunMessages((messages) => ({ ...messages, [id]: 'Requesting run…' }))
    run.mutate({ id, key })
  }

  return (
    <div className="space-y-5 p-6">
      {isCreator ? (
        <form
          className="max-w-xl"
          aria-busy={save.isPending}
          onSubmit={(event) => {
            event.preventDefault()
            if (!scope.isCurrent() || save.isPending) return
            const error = scheduleTimezoneError(draft.timezone)
            setTimezoneError(error)
            setSaveError(null)
            if (error) {
              timezoneInput.current?.focus()
              return
            }
            save.mutate(draft)
          }}
        >
          <fieldset disabled={save.isPending} className="grid gap-3">
            <div className="grid gap-2">
              <Label htmlFor="schedule-name">Name</Label>
              <Input id="schedule-name" required value={draft.name} onChange={scope.effect((event) => setDraft({ ...draft, name: event.target.value }))} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="schedule-cron">Cron</Label>
              <Input id="schedule-cron" required value={draft.cron_expr} onChange={scope.effect((event) => setDraft({ ...draft, cron_expr: event.target.value }))} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="schedule-timezone">IANA timezone</Label>
              <Input id="schedule-timezone" ref={timezoneInput} value={draft.timezone} placeholder="Asia/Shanghai" autoComplete="off"
                aria-invalid={Boolean(timezoneError)} aria-describedby={`schedule-timezone-help${timezoneError ? ' schedule-timezone-error' : ''}`}
                onChange={scope.effect((event) => { setDraft({ ...draft, timezone: event.target.value }); setTimezoneError(null) })} />
              <p id="schedule-timezone-help" className="text-xs text-muted-foreground">Cron runs in this timezone, including daylight saving changes. Use an IANA name such as Asia/Shanghai, America/New_York, or UTC.</p>
              {timezoneError ? <p id="schedule-timezone-error" role="status" className="text-sm text-destructive">{timezoneError}</p> : null}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="schedule-instruction">Instruction</Label>
              <Textarea id="schedule-instruction" value={draft.instruction} onChange={scope.effect((event) => setDraft({ ...draft, instruction: event.target.value }))} />
            </div>
            <div className="flex gap-2">
              <Button type="submit">{save.isPending ? 'Saving...' : editing ? 'Save schedule' : 'Add schedule'}</Button>
              {editing ? <Button type="button" variant="outline" onClick={scope.effect(() => {
                setEditing(null); setDraft(emptyScheduleDraft); setTimezoneError(null); setSaveError(null)
              })}>Cancel edit</Button> : null}
            </div>
          </fieldset>
          {saveError ? <p role="status" className="mt-2 text-sm text-destructive">{saveError}</p> : null}
        </form>
      ) : (
        <p className="text-sm text-muted-foreground">Only the agent creator can add or run schedules.</p>
      )}
      {schedules.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading schedules...</p> : null}
      {schedules.isError ? <div className="space-y-2">
        <p role="status" className="text-sm text-destructive">{userFacingRequestError(schedules.error, 'Unable to load schedules. Try again.')}</p>
        <Button variant="outline" disabled={schedules.isFetching} onClick={scope.effect(() => { void schedules.refetch() })}>Retry</Button>
      </div> : null}
      {schedules.data?.length === 0 ? <p className="text-sm text-muted-foreground">No schedules yet.</p> : null}
      <ul className="space-y-2">
        {(schedules.data ?? []).map((item) => (
          <li key={item.id} aria-label={item.name} className="flex flex-col gap-2 rounded-xl border border-border px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 break-words">
              <p className="text-sm font-medium">{item.name}</p>
              <p className="text-xs text-muted-foreground">
                {item.cron_expr} · {item.timezone} · next {formatScheduleNextRun(item)}
              </p>
              <p aria-live="polite" className="text-sm text-muted-foreground">{runMessages[item.id]}</p>
              {isCreator && acceptedRun?.scheduleId === item.id ? <ScheduleRunReceipt key={acceptedRun.runId}
                agentId={agent.id} scheduleId={item.id} runId={acceptedRun.runId} /> : null}
            </div>
            {isCreator ? (
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={save.isPending || run.isPending} onClick={() => {
                  if (scope.isCurrent()) requestRun(item.id)
                }}>
                  Run
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={save.isPending}
                  onClick={() => {
                    if (!scope.isCurrent()) return
                    setEditing(item)
                    setDraft({ name: item.name, cron_expr: item.cron_expr, instruction: item.instruction, timezone: item.timezone })
                    setTimezoneError(null)
                    setSaveError(null)
                  }}
                >
                  Edit
                </Button>
                <Button size="sm" variant="outline" onClick={scope.effect(() => setHistoryId(item.id))}>
                  History
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={save.isPending || remove.isPending}
                  onClick={() => remove.mutate(item.id)}
                >
                  Delete
                </Button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      {historyId && isCreator ? <ScheduleRunHistory key={historyId} agentId={agent.id} scheduleId={historyId} /> : null}
    </div>
  )
}
