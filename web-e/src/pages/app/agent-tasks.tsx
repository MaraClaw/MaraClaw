import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/hooks/use-auth'
import { canViewTriggerExecutions } from '@/lib/trigger-executions-query'
import { TriggerExecutionHistory } from './trigger-execution-history'
import {
  completeFocus,
  createTask,
  deleteTrigger,
  listFocus,
  listTaskLogs,
  listTasks,
  listTriggers,
  patchTrigger,
  triggerTask,
  upsertFocus,
  type AgentOut,
} from '@/lib/workspace-api'

export function AgentTasksPage() {
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [focusTitle, setFocusTitle] = useState('')
  const [openTask, setOpenTask] = useState<string | null>(null)
  const tasks = useQuery({
    queryKey: ['tasks', agent.id],
    queryFn: () => listTasks(agent.id),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((task) => task.status === 'pending' || task.status === 'doing') ? 4000 : false,
  })
  const logs = useQuery({
    queryKey: ['task-logs', agent.id, openTask],
    queryFn: () => listTaskLogs(agent.id, openTask!),
    enabled: Boolean(openTask),
    refetchInterval: 4000,
  })
  const focus = useQuery({ queryKey: ['focus', agent.id], queryFn: () => listFocus(agent.id) })
  const triggers = useQuery({ queryKey: ['triggers', agent.id], queryFn: () => listTriggers(agent.id) })

  const addTask = useMutation({
    mutationFn: () => createTask(agent.id, { title, type: 'todo' }),
    onSuccess() {
      setTitle('')
      void queryClient.invalidateQueries({ queryKey: ['tasks', agent.id] })
    },
    onError() {
      toast.error('Unable to create task')
    },
  })

  return (
    <div className="grid gap-8 p-6 lg:grid-cols-2">
      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">Tasks</h2>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            if (!scope.isCurrent()) return
            event.preventDefault()
            if (title.trim()) addTask.mutate()
          }}
        >
          <Input value={title} onChange={scope.effect((event) => setTitle(event.target.value))} placeholder="New todo" />
          <Button type="submit">Add</Button>
        </form>
        <ul className="space-y-2">
          {(tasks.data ?? []).map((task) => (
            <li key={task.id} className="rounded-xl border border-border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <button type="button" className="text-left" onClick={scope.effect(() => setOpenTask(task.id))}>
                  <p className="text-sm font-medium">{task.title}</p>
                  <p className="text-xs text-muted-foreground">{task.status ?? task.type}</p>
                </button>
                <Button size="sm" variant="outline" onClick={() => scope.isCurrent() && void triggerTask(agent.id, task.id).catch(scope.rethrowCurrent)}>
                  Run
                </Button>
              </div>
              {openTask === task.id ? (
                <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                  {(logs.data ?? []).map((log, index) => (
                    <li key={log.id ?? String(index)}>{log.content}</li>
                  ))}
                  {(logs.data ?? []).length === 0 ? <li>No logs yet.</li> : null}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">Focus</h2>
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
            }).catch(scope.rethrowCurrent)
          }}
        >
          <Input value={focusTitle} onChange={scope.effect((event) => setFocusTitle(event.target.value))} placeholder="Focus item" />
          <Button type="submit">Add</Button>
        </form>
        <ul className="space-y-2">
          {(focus.data ?? []).map((item) => (
            <li key={item.key} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{item.title}</p>
                <Badge variant="soft">{item.status}</Badge>
              </div>
              {item.status !== 'completed' ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    scope.isCurrent() && void completeFocus(agent.id, item.key).then(scope.effect(() =>
                      queryClient.invalidateQueries({ queryKey: ['focus', agent.id] }),
                    )).catch(scope.rethrowCurrent)
                  }
                >
                  Complete
                </Button>
              ) : null}
            </li>
          ))}
        </ul>

        <h2 className="pt-4 font-display text-lg font-semibold">Triggers</h2>
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
                        )).catch(scope.rethrowCurrent)
                      }
                    />
                    Enabled
                  </label>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      scope.isCurrent() && void deleteTrigger(agent.id, trigger.id).then(scope.effect(() =>
                        queryClient.invalidateQueries({ queryKey: ['triggers', agent.id] }),
                      )).catch(scope.rethrowCurrent)
                    }
                  >
                    Delete
                  </Button>
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
