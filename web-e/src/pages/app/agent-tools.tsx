import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import {
  listAgentToolsWithConfig,
  updateAgentTools,
  type AgentOut,
} from '@/lib/workspace-api'
import { AgentToolConfigForm } from './agent-tool-config'

export function AgentToolsPage() {
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const [openId, setOpenId] = useState<string | null>(null)
  const tools = useQuery({ queryKey: ['tools', agent.id], queryFn: ({ signal }) => listAgentToolsWithConfig(agent.id, signal) })

  const toggle = useMutation({
    mutationFn: (update: { tool_id: string; enabled: boolean }) => updateAgentTools(agent.id, [update]),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['tools', agent.id] })
    },
    onError() {
      toast.error('Unable to update tool')
    },
  })

  return (
    <div className="space-y-3 p-6">
      <p className="text-sm text-muted-foreground">
        Enable tools and edit per-agent config. Secrets stay masked. Network/proxy fields stay with admins.
      </p>
      {tools.isPending ? <p role="status" className="text-sm">Loading tools…</p> : null}
      {tools.isError ? <p role="alert" className="text-sm text-destructive">Could not load tools. Refresh this page to try again.</p> : null}
      {tools.isSuccess && tools.data.length === 0 ? <p className="text-sm text-muted-foreground">No tools are available for this agent.</p> : null}
      <ul className="space-y-2">
        {(tools.data ?? []).map((tool) => (
          <li key={tool.id} className="rounded-xl border border-border px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <button type="button" className="min-h-11 min-w-0 text-left focus-visible:outline-2 focus-visible:outline-ring" aria-expanded={openId === tool.id} onClick={scope.effect(() => setOpenId(openId === tool.id ? null : tool.id))}>
                <p className="text-sm font-medium">{tool.display_name || tool.name}</p>
                <p className="text-xs text-muted-foreground">{tool.description}</p>
              </button>
              <div className="flex items-center gap-2">
                <Badge variant="soft">{tool.category ?? tool.source}</Badge>
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={tool.enabled}
                    disabled={!canManage || toggle.isPending}
                    onChange={(event) => toggle.mutate({ tool_id: tool.id, enabled: event.target.checked })}
                  />
                  On
                </label>
              </div>
            </div>
            {openId === tool.id && canManage ? <AgentToolConfigForm key={`${agent.id}:${tool.id}`} agentId={agent.id} toolId={tool.id} kind={tool.type === 'mcp' ? 'mcp' : tool.category === 'email' ? 'email' : null} /> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}
