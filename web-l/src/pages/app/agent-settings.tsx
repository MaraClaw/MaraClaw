import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import {
  deleteAgent,
  listLlmModels,
  startAgent,
  stopAgent,
  updateAgent,
  type AgentOut,
} from '@/lib/workspace-api'

export function AgentSettingsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const isCreator = user?.id === agent.creator_id
  const canManage = agent.access_level === 'manage' || isCreator
  const models = useQuery({ queryKey: ['llm-models'], queryFn: listLlmModels })
  const [name, setName] = useState(agent.name)
  const [bio, setBio] = useState(agent.bio ?? '')
  const [welcome, setWelcome] = useState(agent.welcome_message ?? '')
  const [modelId, setModelId] = useState(agent.primary_model_id ?? '')
  const [heartbeatEnabled, setHeartbeatEnabled] = useState(agent.heartbeat_enabled ?? true)
  const [heartbeatMinutes, setHeartbeatMinutes] = useState(String(agent.heartbeat_interval_minutes ?? 240))
  const [activeHours, setActiveHours] = useState(agent.heartbeat_active_hours ?? '09:00-18:00')
  const [timezone, setTimezone] = useState(agent.timezone ?? '')
  const [clamped, setClamped] = useState<Array<{ field: string; requested?: number; applied?: number; reason?: string }>>([])

  const save = useMutation({
    mutationFn: () =>
      updateAgent(agent.id, {
        name,
        bio,
        welcome_message: welcome,
        primary_model_id: modelId || null,
        heartbeat_enabled: heartbeatEnabled,
        heartbeat_interval_minutes: Number(heartbeatMinutes) || 240,
        heartbeat_active_hours: activeHours,
        timezone: timezone || null,
      }),
    onSuccess(updated) {
      const extra = (updated as AgentOut & { _clamped_fields?: typeof clamped })._clamped_fields
      setClamped(extra ?? [])
      toast.success(extra?.length ? 'Saved with company limits applied' : 'Agent updated')
      void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
    },
    onError() {
      toast.error('Only the creator or an admin can update this agent')
    },
  })

  return (
    <div className="mx-auto max-w-xl space-y-4 p-6">
      <div className="flex gap-2">
        {canManage ? (
          <>
            <Button size="sm" variant="outline" onClick={() => void startAgent(agent.id).then(() => queryClient.invalidateQueries({ queryKey: ['agent', agent.id] }))}>
              Start
            </Button>
            <Button size="sm" variant="outline" onClick={() => void stopAgent(agent.id).then(() => queryClient.invalidateQueries({ queryKey: ['agent', agent.id] }))}>
              Stop
            </Button>
          </>
        ) : null}
        {isCreator && !agent.is_system ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              if (!window.confirm(`Delete ${agent.name}?`)) return
              void deleteAgent(agent.id).then(() => {
                toast.success('Deleted')
                navigate('/app/agents')
              })
            }}
          >
            Delete
          </Button>
        ) : null}
      </div>

      <div className="space-y-2">
        <Label htmlFor="agent-name">Name</Label>
        <Input id="agent-name" value={name} onChange={(event) => setName(event.target.value)} disabled={!isCreator} />
      </div>
      <div className="space-y-2">
        <Label htmlFor="agent-bio">Bio</Label>
        <Textarea id="agent-bio" value={bio} onChange={(event) => setBio(event.target.value)} disabled={!isCreator} />
      </div>
      <div className="space-y-2">
        <Label htmlFor="agent-welcome">Welcome message</Label>
        <Textarea id="agent-welcome" value={welcome} onChange={(event) => setWelcome(event.target.value)} disabled={!isCreator} />
      </div>
      <div className="space-y-2">
        <Label htmlFor="agent-model">Primary model</Label>
        <select
          id="agent-model"
          className="h-11 w-full rounded-xl border border-input bg-transparent px-3 text-sm"
          value={modelId}
          onChange={(event) => setModelId(event.target.value)}
          disabled={!isCreator}
        >
          <option value="">Tenant default</option>
          {(models.data ?? []).map((model) => (
            <option key={model.id} value={model.id}>
              {model.provider} / {model.model}
            </option>
          ))}
        </select>
      </div>
      <div className="space-y-2">
        <Label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={heartbeatEnabled}
            onChange={(event) => setHeartbeatEnabled(event.target.checked)}
            disabled={!isCreator}
          />
          Heartbeat enabled
        </Label>
        <Label htmlFor="hb-min">Heartbeat interval (minutes)</Label>
        <Input
          id="hb-min"
          value={heartbeatMinutes}
          onChange={(event) => setHeartbeatMinutes(event.target.value)}
          disabled={!isCreator}
        />
        <Label htmlFor="hb-hours">Active hours</Label>
        <Input
          id="hb-hours"
          value={activeHours}
          onChange={(event) => setActiveHours(event.target.value)}
          disabled={!isCreator}
        />
        <Label htmlFor="tz">Timezone</Label>
        <Input id="tz" value={timezone} onChange={(event) => setTimezone(event.target.value)} disabled={!isCreator} />
      </div>
      {clamped.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          Company limits applied:{' '}
          {clamped.map((item) => `${item.field} ${item.requested} → ${item.applied}`).join(', ')}
        </p>
      ) : null}
      {isCreator ? (
        <Button onClick={() => save.mutate()} disabled={save.isPending}>
          Save
        </Button>
      ) : (
        <p className="text-sm text-muted-foreground">You can chat with this agent but not change its settings.</p>
      )}
    </div>
  )
}
