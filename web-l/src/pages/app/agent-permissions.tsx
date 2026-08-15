import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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

export function AgentPermissionsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const isCreator = user?.id === agent.creator_id
  const [scope, setScope] = useState(agent.access_mode === 'private' ? 'private' : agent.access_mode || 'company')
  const [handoverId, setHandoverId] = useState('')

  const perms = useQuery({ queryKey: ['permissions', agent.id], queryFn: () => getAgentPermissions(agent.id) })
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
    mutationFn: () => updateAgentPermissions(agent.id, { scope_type: scope, access_level: 'use' }),
    onSuccess() {
      toast.success('Access updated')
      void queryClient.invalidateQueries({ queryKey: ['permissions', agent.id] })
      void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
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

      {canManage ? (
        <div className="space-y-3">
          <select
            className="h-11 w-full rounded-xl border border-input bg-transparent px-3 text-sm"
            value={scope}
            onChange={(event) => setScope(event.target.value)}
          >
            <option value="private">Private (creator only)</option>
            <option value="company">Whole company</option>
            <option value="custom">Custom list</option>
          </select>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            Save access
          </Button>
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
          <h3 className="text-sm font-semibold">Handover ownership</h3>
          <select
            className="h-11 w-full rounded-xl border border-input bg-transparent px-3 text-sm"
            value={handoverId}
            onChange={(event) => setHandoverId(event.target.value)}
          >
            <option value="">Choose a teammate</option>
            {(candidates.data ?? []).map((row) => (
              <option key={row.id} value={row.id}>
                {row.name || row.email || row.username}
              </option>
            ))}
          </select>
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
