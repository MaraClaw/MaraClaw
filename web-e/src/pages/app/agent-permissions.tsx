import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { AgentDepartmentAccess } from '@/components/agent-department-access'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/hooks/use-auth'
import {
  getAgentPermissions,
  handoverAgent,
  listAgentApprovals,
  listPermissionCandidates,
  resolveAgentApproval,
  updateAgentPermissions,
  type AgentOut,
} from '@/lib/workspace-api'

type AccessDraft = {
  readonly agentId: string
  readonly scopeType: string
  readonly departmentIds: readonly string[]
}

export function AgentPermissionsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const isCreator = user?.id === agent.creator_id
  const [draft, setDraft] = useState<AccessDraft | null>(null)
  const [handoverId, setHandoverId] = useState('')

  const perms = useQuery({ queryKey: ['permissions', agent.id], queryFn: () => getAgentPermissions(agent.id) })
  const canManage = agent.access_level === 'manage' && perms.data?.can_manage !== false
  const fetchedScope = perms.data?.scope_type ?? agent.access_mode ?? 'company'
  const savedScope = fetchedScope === 'user' ? 'private' : fetchedScope
  const currentDraft = draft?.agentId === agent.id ? draft : null
  const scope = currentDraft?.scopeType ?? savedScope
  const savedDepartmentIds = perms.data?.department_ids ?? []
  const departmentIds = currentDraft?.departmentIds ?? savedDepartmentIds
  const isDirty = scope !== savedScope || (scope === 'custom' && (
    departmentIds.length !== savedDepartmentIds.length
    || departmentIds.some((id) => !savedDepartmentIds.includes(id))
  ))
  const candidates = useQuery({
    queryKey: ['perm-candidates', agent.id],
    queryFn: () => listPermissionCandidates(agent.id),
    enabled: canManage,
  })
  const approvals = useQuery({
    queryKey: ['approvals', agent.id],
    queryFn: () => listAgentApprovals(agent.id),
    enabled: isCreator,
  })

  const save = useMutation({
    mutationFn: ({ agentId, body }: { readonly agentId: string; readonly body: Parameters<typeof updateAgentPermissions>[1] }) =>
      updateAgentPermissions(agentId, body),
    async onSuccess(_response, { agentId }) {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['permissions', agentId] }),
        queryClient.invalidateQueries({ queryKey: ['agent', agentId] }),
        queryClient.invalidateQueries({ queryKey: ['agents'] }),
      ])
      setDraft((current) => current?.agentId === agentId ? null : current)
      toast.success('Access updated')
    },
    onError() {
      toast.error('Unable to update access')
    },
  })

  return (
    <div className="mx-auto max-w-xl space-y-6 p-6">
      <div>
        <h2 className="font-display text-lg font-semibold">Who can use this agent</h2>
        <p className="text-sm text-muted-foreground">
          Current access: {perms.data?.effective_access_level ?? agent.access_level}.
        </p>
      </div>

      {perms.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading current access…</p> : null}
      {perms.isError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">Unable to load current access. Reload it before saving changes.</p>
          <Button size="sm" variant="outline" disabled={perms.isFetching} onClick={() => void perms.refetch()}>
            Retry access
          </Button>
        </div>
      ) : null}

      {canManage ? (
        <div className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="agent-access-scope">Who can use this agent</Label>
            <Select
              id="agent-access-scope"
              value={scope}
              disabled={save.isPending || !perms.isSuccess}
              onChange={(event) => setDraft({ agentId: agent.id, scopeType: event.target.value, departmentIds })}
            >
              <option value="private">Private (creator only)</option>
              <option value="company">Whole company</option>
              <option value="custom">Custom list</option>
            </Select>
          </div>
          <AgentDepartmentAccess
            key={agent.id}
            agentId={agent.id}
            departmentIds={departmentIds}
            departmentAccess={perms.data?.department_access ?? []}
            isCustom={scope === 'custom'}
            disabled={save.isPending || !perms.isSuccess}
            onChange={(ids) => setDraft({ agentId: agent.id, scopeType: scope, departmentIds: ids })}
          />
          <Button
            disabled={save.isPending || !perms.isSuccess || perms.isFetching || !isDirty}
            aria-describedby="agent-access-save-status"
            onClick={() => {
              if (!perms.data || !perms.isSuccess || perms.isFetching) return
              save.mutate({
                agentId: agent.id,
                body: {
                  scope_type: scope,
                  access_level: perms.data.access_level ?? 'use',
                  scope_ids: perms.data.scope_ids ?? [],
                  user_access: (perms.data.user_access ?? []).map((row) => ({ id: row.id, access_level: row.access_level })),
                  department_ids: scope === 'custom' ? departmentIds : [],
                },
              })
            }}
          >
            Save access
          </Button>
          <p id="agent-access-save-status" role="status" className="text-sm text-muted-foreground">
            {save.isPending
              ? 'Saving access…'
              : perms.isFetching
                ? 'Refreshing current access…'
                : isDirty
                  ? 'You have unsaved changes.'
                  : ''}
          </p>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Only managers can change who has access.</p>
      )}

      <ul className="space-y-2">
        {(perms.data?.user_access ?? []).map((row) => (
          <li key={row.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2 text-sm">
            <span>{row.name || row.email || row.id}</span>
            <Badge variant="soft">{row.access_level}</Badge>
          </li>
        ))}
      </ul>

      {isCreator ? (
        <div className="space-y-2">
          <Label htmlFor="agent-handover" className="text-sm font-semibold">
            Handover ownership
          </Label>
          <Select id="agent-handover" value={handoverId} onChange={(event) => setHandoverId(event.target.value)}>
            <option value="">Choose a teammate</option>
            {(candidates.data ?? []).map((row) => (
              <option key={row.id} value={row.id}>
                {row.name || row.email || row.username}
              </option>
            ))}
          </Select>
          <Button
            variant="outline"
            disabled={!handoverId}
            onClick={() =>
              void handoverAgent(agent.id, handoverId).then(() => {
                toast.success('Ownership transferred')
                void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
              })
            }
          >
            Transfer
          </Button>
        </div>
      ) : null}

      {isCreator ? (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">Approvals</h3>
          {(approvals.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No approval requests.</p>
          ) : (
            <ul className="space-y-2">
              {(approvals.data ?? []).map((item) => (
                <li key={item.id} className="rounded-xl border border-border px-3 py-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span>{item.action_type ?? 'action'}</span>
                    <Badge variant="soft">{item.status}</Badge>
                  </div>
                  {item.status === 'pending' ? (
                    <div className="mt-2 flex gap-2">
                      <Button
                        size="sm"
                        onClick={() =>
                          void resolveAgentApproval(agent.id, item.id, 'approve').then(() =>
                            queryClient.invalidateQueries({ queryKey: ['approvals', agent.id] }),
                          )
                        }
                      >
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          void resolveAgentApproval(agent.id, item.id, 'reject').then(() =>
                            queryClient.invalidateQueries({ queryKey: ['approvals', agent.id] }),
                          )
                        }
                      >
                        Reject
                      </Button>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  )
}
