import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  deleteRelationship,
  listRelationshipCandidates,
  listRelationships,
  saveRelationships,
  type AgentOut,
} from '@/lib/workspace-api'

const RELATIONS = ['collaborator', 'direct_leader', 'team_member', 'stakeholder', 'subordinate', 'mentor', 'other']

export function AgentRelationshipsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const canManage = agent.access_level === 'manage'
  const queryClient = useQueryClient()
  const [memberId, setMemberId] = useState('')
  const [relation, setRelation] = useState('collaborator')
  const rows = useQuery({ queryKey: ['rels', agent.id], queryFn: () => listRelationships(agent.id) })
  const candidates = useQuery({
    queryKey: ['rel-candidates', agent.id],
    queryFn: () => listRelationshipCandidates(agent.id),
    enabled: canManage,
  })

  const add = useMutation({
    mutationFn: async () => {
      const current = (rows.data ?? []).map((row) => ({
        member_id: row.member_id,
        relation: row.relation,
        description: row.description ?? '',
      }))
      current.push({ member_id: memberId, relation, description: '' })
      await saveRelationships(agent.id, current)
    },
    onSuccess() {
      setMemberId('')
      void queryClient.invalidateQueries({ queryKey: ['rels', agent.id] })
      toast.success('Relationship saved')
    },
    onError() {
      toast.error('Unable to save relationship')
    },
  })

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-6">
      <h2 className="font-display text-lg font-semibold">People this agent works with</h2>
      {canManage ? (
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (memberId) add.mutate()
          }}
        >
          <select
            className="h-11 min-w-48 flex-1 rounded-xl border border-input bg-transparent px-3 text-sm"
            value={memberId}
            onChange={(event) => setMemberId(event.target.value)}
          >
            <option value="">Choose a person</option>
            {(candidates.data ?? []).map((row) => (
              <option key={row.id} value={row.id}>
                {row.name || row.email}
              </option>
            ))}
          </select>
          <select
            className="h-11 rounded-xl border border-input bg-transparent px-3 text-sm"
            value={relation}
            onChange={(event) => setRelation(event.target.value)}
          >
            {RELATIONS.map((item) => (
              <option key={item} value={item}>
                {item.replaceAll('_', ' ')}
              </option>
            ))}
          </select>
          <Button type="submit">Add</Button>
        </form>
      ) : null}
      <ul className="space-y-2">
        {(rows.data ?? []).map((row) => (
          <li key={row.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
            <div>
              <p className="text-sm font-medium">{row.member?.name || row.member_id}</p>
              <p className="text-xs text-muted-foreground">{row.member?.email}</p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="soft">{row.relation_label || row.relation}</Badge>
              {canManage ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    void deleteRelationship(agent.id, row.id).then(() =>
                      queryClient.invalidateQueries({ queryKey: ['rels', agent.id] }),
                    )
                  }
                >
                  Remove
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
