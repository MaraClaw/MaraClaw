import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { listAgentToolsWithConfig, listFiles, updateAgentTools, type AgentOut } from '@/lib/workspace-api'

const BROWSER_TOOLS = [
  'agentbay_browser_navigate',
  'agentbay_browser_screenshot',
  'agentbay_browser_click',
  'agentbay_browser_type',
  'agentbay_browser_extract',
  'agentbay_browser_observe',
  'agentbay_browser_login',
]

export function AgentPlaywrightPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const tools = useQuery({ queryKey: ['tools', agent.id], queryFn: () => listAgentToolsWithConfig(agent.id) })
  const crabbox = useQuery({
    queryKey: ['files', agent.id, '.crabbox'],
    queryFn: () => listFiles(agent.id, '.crabbox'),
    retry: false,
  })
  const reports = useQuery({
    queryKey: ['files', agent.id, '.crabbox/captures'],
    queryFn: () => listFiles(agent.id, '.crabbox/captures'),
    retry: false,
  })

  const browserTools = (tools.data ?? []).filter(
    (tool) => BROWSER_TOOLS.includes(tool.name) || tool.name.toLowerCase().includes('playwright'),
  )

  const toggle = useMutation({
    mutationFn: (update: { tool_id: string; enabled: boolean }) => updateAgentTools(agent.id, [update]),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['tools', agent.id] })
    },
    onError() {
      toast.error('Unable to update browser tools')
    },
  })

  return (
    <div className="space-y-5 p-6">
      <div>
        <h2 className="font-display text-lg font-semibold">Playwright / browser</h2>
        <p className="text-sm text-muted-foreground">
          AgentBay browser tools run Playwright inside the remote session. Crabbox captures land in the workspace when
          gogcli browser tests run.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" asChild>
          <Link to={`/app/agents/${agent.id}/control`}>Take Control</Link>
        </Button>
        <Button size="sm" variant="outline" asChild>
          <Link to={`/app/agents/${agent.id}/credentials`}>Cookie vault</Link>
        </Button>
      </div>
      <ul className="space-y-2">
        {browserTools.map((tool) => (
          <li key={tool.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
            <div>
              <p className="text-sm font-medium">{tool.display_name || tool.name}</p>
              <p className="text-xs text-muted-foreground">{tool.description}</p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="soft">{tool.enabled ? 'On' : 'Off'}</Badge>
              <label className="text-xs">
                <input
                  type="checkbox"
                  checked={tool.enabled}
                  disabled={!canManage || toggle.isPending}
                  onChange={(event) => toggle.mutate({ tool_id: tool.id, enabled: event.target.checked })}
                />
              </label>
            </div>
          </li>
        ))}
      </ul>
      {browserTools.length === 0 ? (
        <p className="text-sm text-muted-foreground">No browser tools are installed on this agent yet.</p>
      ) : null}

      <section>
        <h3 className="text-sm font-semibold">Crabbox / Playwright reports</h3>
        {crabbox.isError ? (
          <p className="mt-2 text-sm text-muted-foreground">No `.crabbox` folder in this workspace.</p>
        ) : (
          <ul className="mt-2 space-y-1 text-sm">
            {(reports.data ?? crabbox.data ?? []).map((file) => (
              <li key={file.path} className="rounded-lg border border-border px-3 py-1.5">
                {file.path} {file.is_dir ? '/' : ''}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
