import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  createSchedule,
  deleteSchedule,
  listScheduleHistory,
  listSchedules,
  runSchedule,
  updateSchedule,
  type AgentOut,
} from '@/lib/workspace-api'
import { useAuth } from '@/hooks/use-auth'

export function AgentSchedulesPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const isCreator = user?.id === agent.creator_id
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [cron, setCron] = useState('0 9 * * 1-5')
  const [instruction, setInstruction] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [historyId, setHistoryId] = useState<string | null>(null)
  const schedules = useQuery({ queryKey: ['schedules', agent.id], queryFn: () => listSchedules(agent.id) })
  const history = useQuery({
    queryKey: ['schedule-history', agent.id, historyId],
    queryFn: () => listScheduleHistory(agent.id, historyId!),
    enabled: Boolean(historyId),
  })

  const create = useMutation({
    mutationFn: () => createSchedule(agent.id, { name, cron_expr: cron, instruction, is_enabled: true }),
    onSuccess() {
      setName('')
      setInstruction('')
      void queryClient.invalidateQueries({ queryKey: ['schedules', agent.id] })
    },
    onError() {
      toast.error('Unable to create schedule. Only the creator can add cron jobs.')
    },
  })

  return (
    <div className="space-y-5 p-6">
      {isCreator ? (
        <form
          className="grid max-w-xl gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (editingId) {
              void updateSchedule(agent.id, editingId, {
                name,
                cron_expr: cron,
                instruction,
              }).then(() => {
                setEditingId(null)
                setName('')
                setInstruction('')
                void queryClient.invalidateQueries({ queryKey: ['schedules', agent.id] })
              })
              return
            }
            create.mutate()
          }}
        >
          <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Name" />
          <Input value={cron} onChange={(event) => setCron(event.target.value)} placeholder="Cron" />
          <Textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="Instruction" />
          <Button type="submit">{editingId ? 'Save schedule' : 'Add schedule'}</Button>
        </form>
      ) : (
        <p className="text-sm text-muted-foreground">Only the agent creator can add or run schedules.</p>
      )}
      <ul className="space-y-2">
        {(schedules.data ?? []).map((item) => (
          <li key={item.id} className="flex flex-col gap-2 rounded-xl border border-border px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm font-medium">{item.name}</p>
              <p className="text-xs text-muted-foreground">
                {item.cron_expr} · next {item.next_run_at ?? '—'}
              </p>
            </div>
            {isCreator ? (
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={() => void runSchedule(agent.id, item.id)}>
                  Run
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setEditingId(item.id)
                    setName(item.name)
                    setCron(item.cron_expr)
                    setInstruction(item.instruction)
                  }}
                >
                  Edit
                </Button>
                <Button size="sm" variant="outline" onClick={() => setHistoryId(item.id)}>
                  History
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    void deleteSchedule(agent.id, item.id).then(() =>
                      queryClient.invalidateQueries({ queryKey: ['schedules', agent.id] }),
                    )
                  }
                >
                  Delete
                </Button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      {historyId ? (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">Run history</h3>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {(history.data ?? []).map((row) => (
              <li key={row.id}>
                {row.created_at?.slice(0, 19)} · {row.summary || row.reply || 'ran'}
              </li>
            ))}
            {(history.data ?? []).length === 0 ? <li>No history yet.</li> : null}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
