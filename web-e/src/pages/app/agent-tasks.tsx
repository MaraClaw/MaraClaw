import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [focusTitle, setFocusTitle] = useState('')
  const [openTask, setOpenTask] = useState<string | null>(null)
  const tasks = useQuery({
    queryKey: ['tasks', agent.id],
    queryFn: () => listTasks(agent.id),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((task) => task.status === 'pending' || task.status === 'running') ? 4000 : false,
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
            event.preventDefault()
            if (title.trim()) addTask.mutate()
          }}
        >
          <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="New todo" />
          <Button type="submit">Add</Button>
        </form>
        <ul className="space-y-2">
          {(tasks.data ?? []).map((task) => (
            <li key={task.id} className="rounded-xl border border-border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <button type="button" className="text-left" onClick={() => setOpenTask(task.id)}>
                  <p className="text-sm font-medium">{task.title}</p>
                  <p className="text-xs text-muted-foreground">{task.status ?? task.type}</p>
                </button>
                <Button size="sm" variant="outline" onClick={() => void triggerTask(agent.id, task.id)}>
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
            void upsertFocus(agent.id, {
              key: focusTitle.toLowerCase().replace(/\s+/g, '-'),
              title: focusTitle,
              status: 'in_progress',
              kind: 'normal',
            }).then(() => {
              setFocusTitle('')
              void queryClient.invalidateQueries({ queryKey: ['focus', agent.id] })
            })
          }}
        >
          <Input value={focusTitle} onChange={(event) => setFocusTitle(event.target.value)} placeholder="Focus item" />
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
                    void completeFocus(agent.id, item.key).then(() =>
                      queryClient.invalidateQueries({ queryKey: ['focus', agent.id] }),
                    )
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
            <li key={trigger.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2 text-sm">
              <span>{trigger.name}</span>
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={trigger.is_enabled}
                    onChange={(event) =>
                      void patchTrigger(agent.id, trigger.id, { is_enabled: event.target.checked }).then(() =>
                        queryClient.invalidateQueries({ queryKey: ['triggers', agent.id] }),
                      )
                    }
                  />
                  Enabled
                </label>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    void deleteTrigger(agent.id, trigger.id).then(() =>
                      queryClient.invalidateQueries({ queryKey: ['triggers', agent.id] }),
                    )
                  }
                >
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
