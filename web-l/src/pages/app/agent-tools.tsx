import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  getAgentToolConfig,
  listAgentToolsWithConfig,
  saveAgentToolConfig,
  testEmailConfig,
  testMcpServer,
  updateAgentTools,
  type AgentOut,
} from '@/lib/workspace-api'

const BLOCKED = new Set(['allow_network', 'http_proxy', 'https_proxy', 'no_proxy'])

export function AgentToolsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const [openId, setOpenId] = useState<string | null>(null)
  const tools = useQuery({ queryKey: ['tools', agent.id], queryFn: () => listAgentToolsWithConfig(agent.id) })

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
      <ul className="space-y-2">
        {(tools.data ?? []).map((tool) => (
          <li key={tool.id} className="rounded-xl border border-border px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <button type="button" className="text-left" onClick={() => setOpenId(openId === tool.id ? null : tool.id)}>
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
            {openId === tool.id && canManage ? <ToolConfigForm agentId={agent.id} toolId={tool.id} /> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ToolConfigForm({ agentId, toolId }: { agentId: string; toolId: string }) {
  const config = useQuery({
    queryKey: ['tool-config', agentId, toolId],
    queryFn: () => getAgentToolConfig(agentId, toolId),
  })
  const [draft, setDraft] = useState<Record<string, string>>({})
  const schema = (config.data?.config_schema?.properties ?? {}) as Record<string, { type?: string; title?: string }>
  const keys = Object.keys(schema).filter((key) => !BLOCKED.has(key))

  const merged = { ...(config.data?.merged_config ?? {}), ...draft }

  return (
    <div className="mt-3 space-y-2 border-t border-border pt-3">
      {keys.length === 0 ? <p className="text-xs text-muted-foreground">No editable fields.</p> : null}
      {keys.map((key) => (
        <label key={key} className="block text-xs">
          <span className="text-muted-foreground">{schema[key]?.title || key}</span>
          <Input
            className="mt-1 h-9"
            type={key.toLowerCase().includes('password') || key.toLowerCase().includes('secret') || key === 'api_key' ? 'password' : 'text'}
            value={String(merged[key] ?? '')}
            onChange={(event) => setDraft((prev) => ({ ...prev, [key]: event.target.value }))}
          />
        </label>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          onClick={() =>
            void saveAgentToolConfig(agentId, toolId, { ...config.data?.merged_config, ...draft }).then(() =>
              toast.success('Saved'),
            )
          }
        >
          Save config
        </Button>
        {merged.api_key || merged.host || merged.username ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              void testEmailConfig(merged).then((result) =>
                toast[result.ok ? 'success' : 'error'](result.ok ? 'Email connection ok' : (result.error ?? 'Failed')),
              )
            }
          >
            Test email
          </Button>
        ) : null}
        {typeof merged.server_url === 'string' || typeof merged.url === 'string' ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              void testMcpServer(String(merged.server_url || merged.url), String(merged.api_key || '')).then((result) =>
                toast[result.ok ? 'success' : 'error'](result.ok ? 'MCP reachable' : (result.error ?? 'Failed')),
              )
            }
          >
            Test MCP
          </Button>
        ) : null}
      </div>
    </div>
  )
}
