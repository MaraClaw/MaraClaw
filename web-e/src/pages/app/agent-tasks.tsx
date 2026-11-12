import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { useInfiniteQuery, useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useEffect, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
import { userFacingRequestError } from '@/lib/http'
import { showRequestError } from '@/lib/request-error-toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/hooks/use-auth'
import { canViewTriggerExecutions } from '@/lib/trigger-executions-query'
import { canRunTask } from '@/lib/task-run'
import { doingTasksQuery, mergeTasks, retainTaskLogTail, taskHistoryQuery, taskLogsQuery, taskLogTailQuery } from '@/lib/task-query'
import type { AgentOut } from '@/lib/agents-api'
import { TriggerExecutionHistory } from './trigger-execution-history'
import {
  completeFocus,
  createTask,
  deleteTrigger,
  listFocus,
  getTask,
  listTriggers,
  patchTrigger,
  triggerTask,
  upsertFocus,
} from '@/lib/agent-work-api'

export function AgentTasksPage() {
  const { t: translate, owned } = useTranslation()
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [focusTitle, setFocusTitle] = useState('')
  const [openTask, setOpenTask] = useState<string | null>(null)
  const history = useInfiniteQuery(taskHistoryQuery(scope, agent.id))
  const doingOptions = doingTasksQuery(scope, agent.id)
  const doing = useQuery(doingOptions)
  const tasks = mergeTasks(history.data?.pages.flatMap((page) => page.items) ?? [], doing.data ?? [])
  const opened = tasks.find((task) => task.id === openTask)
  const logTarget = opened ? { agentId: agent.id, taskId: opened.id } : null
  const logs = useInfiniteQuery(taskLogsQuery(scope, logTarget))
  const { refetch: refreshLogHistory } = logs
  const lastLogPage = logs.data?.pages.at(-1)
  const tail = useQuery(taskLogTailQuery(scope, logTarget, opened && lastLogPage ? {
    cursor: logs.data?.pageParams.at(-1) ?? null, page: lastLogPage, status: opened.status,
  } : null))
  const { refetch: refreshLogs } = tail
  const logItems = [...new Map(logs.data?.pages.flatMap((page, index, pages) => index === pages.length - 1 ? tail.data?.items ?? page.items : page.items).map((log) => [log.id, log])).values()]
  const hasMoreLogs = tail.data ? Boolean(tail.data.next_cursor) : logs.hasNextPage
  const previousOpen = useRef<{ agentId: string; taskId: string; status: string } | null>(null)
  const finalLogs = useRef(false)
  useEffect(() => {
    const previous = previousOpen.current
    if (previous?.agentId !== agent.id || previous?.taskId !== opened?.id) finalLogs.current = false
    if (opened && previous?.agentId === agent.id && previous.taskId === opened.id && previous.status === 'doing' && opened.status === 'done') {
      finalLogs.current = true
    }
    if (finalLogs.current && !logs.isFetching && !tail.isFetching) {
      finalLogs.current = false
      if (lastLogPage) void refreshLogs()
      else void refreshLogHistory()
    }
    previousOpen.current = opened ? { agentId: agent.id, taskId: opened.id, status: opened.status } : null
  }, [agent.id, opened, refreshLogs, refreshLogHistory, lastLogPage, logs.isFetching, tail.isFetching])
  const focus = useQuery({ queryKey: ['focus', agent.id], queryFn: () => listFocus(agent.id) })
  const triggers = useQuery({ queryKey: ['triggers', agent.id], queryFn: () => listTriggers(agent.id) })

  const addTask = useMutation({
    mutationFn: () => createTask(agent.id, { title, type: 'todo' }),
    onSuccess(task) {
      setTitle('')
      queryClient.setQueryData(doingOptions.queryKey, (previous) => mergeTasks(previous ?? [], [task]))
    },
    onError() {
      toast.error(translate("Unable to create task"))
    },
  })
  const runTask = useMutation({
    mutationFn: async (taskId: string) => {
      await triggerTask(agent.id, taskId)
      return getTask({ agentId: agent.id, taskId }, { owner: scope.owner })
    },
    onSuccess(task) {
      queryClient.setQueryData(doingOptions.queryKey, (previous) => mergeTasks(previous ?? [], [task]))
    },
    onError(error) {
      toast.error(userFacingRequestError(error, translate('Unable to run task')))
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: doingOptions.queryKey })
    },
  })

  return (
    <div className="grid gap-8 p-6 lg:grid-cols-2">
      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">{translate("Tasks")}</h2>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            if (!scope.isCurrent()) return
            event.preventDefault()
            if (title.trim()) addTask.mutate()
          }}
        >
          <Input value={title} onChange={scope.effect((event) => setTitle(event.target.value))} placeholder={translate("New todo")} />
          <Button type="submit">{translate("Add")}</Button>
        </form>
        {history.isPending ? <p className="text-sm text-muted-foreground">{translate("Loading tasks...")}</p> : null}
        {history.isError || doing.isError ? <div><RequestErrorToast error={history.error ?? doing.error} message="Unable to load tasks." /><button type="button" className="underline" onClick={scope.effect(() => { if (history.isError) void history.refetch(); if (doing.isError) void doing.refetch() })}>{translate("Retry tasks")}</button></div> : null}
        <ul className="space-y-2">
          {tasks.map((task) => (
            <li key={task.id} className="rounded-xl border border-border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <button type="button" className="text-left" aria-expanded={openTask === task.id} onClick={scope.effect(() => setOpenTask(openTask === task.id ? null : task.id))}>
                  <p className="text-sm font-medium">{task.title}</p>
                <p className="text-xs text-muted-foreground">{owned(task.status ?? task.type)}</p>
                </button>
                <Button size="sm" variant="outline" disabled={!canRunTask(task.status)} onClick={scope.effect(() => runTask.mutate(task.id))}>
                  {translate("Run")}</Button>
              </div>
              {openTask === task.id ? (
                <div className="mt-2 space-y-2 text-xs text-muted-foreground">
                  {logs.isPending ? <p>{translate("Loading logs...")}</p> : null}
                  {logs.isError || tail.isError ? <div><RequestErrorToast error={logs.error ?? tail.error} message="Unable to load logs." /><button type="button" className="underline" onClick={scope.effect(() => { if (logs.isError) void logs.refetch(); if (tail.isError) void refreshLogs() })}>{translate("Retry logs")}</button></div> : null}
                  <ul className="space-y-1">{logItems.map((log) => <li key={log.id}>{log.content}</li>)}</ul>
                  {logs.isSuccess && logItems.length === 0 ? <p>{translate("No logs yet.")}</p> : null}
                  {hasMoreLogs || (logs.data?.pages.length ?? 0) > 1 ? <Button size="sm" variant="outline"
                    aria-disabled={logs.isFetching || tail.isFetching || !hasMoreLogs}
                    onClick={scope.effect(() => {
                      if (logs.isFetching || tail.isFetching || !hasMoreLogs) return
                      retainTaskLogTail(scope, { agentId: agent.id, taskId: task.id })
                      void logs.fetchNextPage()
                    })}>
                    {logs.isFetchingNextPage ? 'Loading...' : hasMoreLogs ? translate("Load more logs") : translate("All logs loaded")}
                  </Button> : null}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
        {history.isSuccess && tasks.length === 0 ? <p className="text-sm text-muted-foreground">{translate("No tasks yet.")}</p> : null}
        {history.hasNextPage || (history.data?.pages.length ?? 0) > 1 ? <Button size="sm" variant="outline"
          aria-disabled={history.isFetching || !history.hasNextPage}
          onClick={scope.effect(() => {
            if (history.isFetching || !history.hasNextPage) return
            void history.fetchNextPage()
          })}>
          {history.isFetchingNextPage ? 'Loading...' : history.hasNextPage ? translate("Load older tasks") : translate("All tasks loaded")}
        </Button> : null}
      </section>

      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">{translate("Focus")}</h2>
        {focus.isError ? <RequestErrorToast error={focus.error} message="Unable to load focus items. Try again." /> : null}
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!focusTitle.trim()) return
            if (!scope.isCurrent()) return
            void upsertFocus(agent.id, {
              key: focusTitle.toLowerCase().replace(/\s+/g, '-'),
              title: focusTitle,
              description: focusTitle,
              status: 'in_progress',
              kind: 'normal',
            }).then(() => {
              if (!scope.isCurrent()) return
              setFocusTitle('')
              void queryClient.invalidateQueries({ queryKey: ['focus', agent.id] })
            }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to add this focus item.")), error)))
          }}
        >
          <Input value={focusTitle} onChange={scope.effect((event) => setFocusTitle(event.target.value))} placeholder={translate("Focus item")} />
          <Button type="submit">{translate("Add")}</Button>
        </form>
        <ul className="space-y-2">
          {(focus.data ?? []).map((item) => (
            <li key={item.key} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{item.title}</p>
                  <Badge variant="soft">{owned(item.status)}</Badge>
              </div>
              {item.status !== 'completed' ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    scope.isCurrent() && void completeFocus(agent.id, item.key).then(scope.effect(() =>
                      queryClient.invalidateQueries({ queryKey: ['focus', agent.id] }),
                    )).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to complete this focus item.")), error)))
                  }
                >
                  {translate("Complete")}</Button>
              ) : null}
            </li>
          ))}
        </ul>

        <h2 className="pt-4 font-display text-lg font-semibold">{translate("Triggers")}</h2>
        {triggers.isError ? <RequestErrorToast error={triggers.error} message="Unable to load triggers. Try again." /> : null}
        <ul className="space-y-2">
          {(triggers.data ?? []).map((trigger) => (
            <li key={`${agent.id}:${trigger.id}`} className="min-w-0 rounded-xl border border-border px-3 py-2 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="min-w-0 wrap-anywhere">{trigger.name}</span>
                <div className="flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={trigger.is_enabled}
                      onChange={(event) =>
                        scope.isCurrent() && void patchTrigger(agent.id, trigger.id, { is_enabled: event.target.checked }).then(scope.effect(() =>
                          queryClient.invalidateQueries({ queryKey: ['triggers', agent.id] }),
                        )).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to update this trigger.")), error)))
                      }
                    />
                    {translate("Enabled")}</label>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      scope.isCurrent() && void deleteTrigger(agent.id, trigger.id).then(scope.effect(() =>
                        queryClient.invalidateQueries({ queryKey: ['triggers', agent.id] }),
                      )).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to delete this trigger.")), error)))
                    }
                  >
                    {translate("Delete")}</Button>
                </div>
              </div>
              {canViewTriggerExecutions(agent, user) ? <TriggerExecutionHistory
                target={{ agentId: agent.id, triggerId: trigger.id }} name={trigger.name}
              /> : null}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
