import * as Dialog from '@radix-ui/react-dialog'
import { useCallback, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { McpServerEditor } from '@/components/mcp-server-editor'
import { Button } from '@/components/ui/button'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { listAgentMcpServers, removeAgentMcpServer, type McpServerList, type McpServerSummary } from '@/lib/agent-mcp-api'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { showRequestError } from '@/lib/request-error-toast'
import type { AgentOut } from '@/lib/workspace-api'

const transportLabels = { 'streamable-http': 'HTTP', sse: 'SSE', stdio: 'Local command (stdio)' } as const

export function AgentMcpPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  return <AgentMcpWorkspace key={`${agent.id}:${agent.access_level}`} agent={agent} />
}

function AgentMcpWorkspace({ agent }: { readonly agent: AgentOut }) {
  const scope = useWorkspaceSession()
  const manager = agent.access_level === 'manage'
  const [editor, setEditor] = useState<{ readonly server: McpServerSummary | null; readonly returnFocus: HTMLElement } | null>(null)
  const [removing, setRemoving] = useState<McpServerSummary | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const addButton = useRef<HTMLButtonElement>(null)
  const removeButton = useRef<HTMLElement | null>(null)
  const retireAccess = useCallback(() => {
    if (!scope.isCurrent()) return
    scope.client.removeQueries({ queryKey: ['agent-mcp-servers', agent.id] })
    scope.client.setQueryData<AgentOut>(['agent', agent.id], (current) => current ? { ...current, access_level: 'use' } : current)
  }, [scope, agent.id])
  const servers = useQuery({
    queryKey: ['agent-mcp-servers', agent.id],
    async queryFn({ signal }) {
      try { return await listAgentMcpServers(agent.id, { owner: scope.owner, signal }) }
      catch (error) {
        if (error instanceof ApiError && error.status === 403 && scope.isCurrent()) {
          showRequestError('Manage access is required to configure MCP servers.', error)
          retireAccess()
        }
        throw error
      }
    },
    enabled: manager,
    retry: false,
    staleTime: 0,
  })
  const remove = useMutation({
    mutationFn: (name: string) => removeAgentMcpServer({ agentId: agent.id, name }, { owner: scope.owner }),
    async onSuccess(result, name) {
      await scope.client.cancelQueries({ queryKey: ['agent-mcp-servers', agent.id] })
      if (!scope.isCurrent() || scope.client.getQueryData<AgentOut>(['agent', agent.id])?.access_level !== 'manage') return
      scope.client.setQueryData<McpServerList>(['agent-mcp-servers', agent.id], (previous) => ({ servers: (previous?.servers ?? []).filter((item) => item.name !== name) }))
      setRemoving(null)
      switch (result.sync) {
        case 'applied': setNotice(null); break
        case 'pending':
          setNotice('Server removed. Sync to OpenClaw is pending.')
          void scope.client.invalidateQueries({ queryKey: ['agent-mcp-servers', agent.id] })
          break
        default: result satisfies never
      }
    },
    onError(error) {
      if (error instanceof ApiError && error.status === 403) {
        showRequestError('Manage access is required to remove MCP servers.', error)
        retireAccess()
      }
    },
  })
  const denied = (servers.error instanceof ApiError && servers.error.status === 403) || (remove.error instanceof ApiError && remove.error.status === 403)
  const canManage = manager && !denied
  return (
    <section aria-labelledby="mcp-heading" className="min-w-0 space-y-6 p-5 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 id="mcp-heading" className="font-display text-2xl font-semibold">MCP</h1>
        {canManage ? <Button ref={addButton} disabled={!servers.isSuccess || servers.fetchStatus === 'paused'} onClick={scope.effect((event) => setEditor({ server: null, returnFocus: event.currentTarget }))}>Add server</Button> : null}
      </div>
      {canManage ? <p className="text-base text-muted-foreground">Stored configurations are synced to OpenClaw. A missing guest configuration does not remove your stored servers.</p> : null}
      {!canManage ? <p className="text-base text-muted-foreground">Manage access is required to view and configure this agent’s MCP servers.</p> : <>
        {notice ? <p role="status" aria-live="polite" aria-atomic="true" className="text-base text-muted-foreground">{notice}</p> : null}
        {servers.fetchStatus === 'paused' ? <p role="status" className="text-base text-muted-foreground">You’re offline. Connect to load MCP servers.</p>
          : servers.isPending ? <p role="status" className="text-base text-muted-foreground">Loading MCP servers...</p> : null}
        {servers.isError ? <div className="space-y-3">
          <RequestErrorToast error={servers.error} message={userFacingRequestError(servers.error, 'Unable to load MCP servers. Check your connection and try again.')} />
          <Button variant="outline" disabled={servers.isFetching} onClick={scope.effect(() => { void servers.refetch() })}>Retry servers</Button>
        </div> : null}
        {servers.data?.servers.length === 0 ? <div className="space-y-2 rounded-2xl border border-border bg-card p-6">
          <h2 className="font-display text-xl font-semibold">No MCP servers</h2>
          <p className="text-base text-muted-foreground">Add a server to connect this agent to MCP tools and resources.</p>
        </div> : null}
        {servers.data && servers.data.servers.length > 0 ? <ul aria-label="Installed MCP servers" className="divide-y divide-border rounded-2xl border border-border bg-card">
          {servers.data.servers.map((server) => <li key={server.name} className="flex min-w-0 flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 space-y-2">
              <h2 className="break-all font-display text-xl font-semibold">{server.name}</h2>
              <p className="text-base text-muted-foreground">{transportLabels[server.transport]} · {server.enabled ? 'Enabled' : 'Disabled'}</p>
              {server.url_configured ? <p className="text-base">URL stored</p> : null}
              {server.command_configured ? <p className="text-base">Command stored · {server.args_count} {server.args_count === 1 ? 'argument' : 'arguments'}</p> : null}
              {server.header_names.length > 0 ? <p className="break-words text-sm text-muted-foreground">Headers: {server.header_names.join(', ')}</p> : null}
              {server.env_names.length > 0 ? <p className="break-words text-sm text-muted-foreground">Environment variables: {server.env_names.join(', ')}</p> : null}
            </div>
            <div className="flex shrink-0 flex-wrap gap-3">
              <Button variant="outline" aria-label={`Configure ${server.name}`} onClick={scope.effect((event) => setEditor({ server, returnFocus: event.currentTarget }))}>Configure</Button>
              <Button variant="outline" className="text-destructive" aria-label={`Remove ${server.name}`} onClick={(event) => { if (!scope.isCurrent()) return; removeButton.current = event.currentTarget; remove.reset(); setRemoving(server) }}>Remove</Button>
            </div>
          </li>)}
        </ul> : null}
        {editor ? <McpServerEditor agentId={agent.id} server={editor.server} returnFocus={editor.returnFocus} onAccessDenied={retireAccess} onClose={() => setEditor(null)} onSavedSyncPending={() => setNotice('Server saved. Sync to OpenClaw is pending.')} /> : null}
        <Dialog.Root open={removing !== null} onOpenChange={scope.effect((open) => { if (!open && !remove.isPending) setRemoving(null) })}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
            <Dialog.Content onCloseAutoFocus={(event) => { event.preventDefault(); (removeButton.current?.isConnected ? removeButton.current : addButton.current)?.focus() }}
              className="fixed top-1/2 left-1/2 z-50 w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-2xl border border-border bg-card p-6 shadow-elevated">
              <Dialog.Title className="font-display text-xl font-semibold">Remove server?</Dialog.Title>
              <Dialog.Description className="break-words text-base text-muted-foreground">Remove the stored configuration for {removing?.name}? This change is synced to OpenClaw.</Dialog.Description>
              {remove.isError ? <RequestErrorToast error={remove.error} message={userFacingRequestError(remove.error, 'Unable to remove this server. Check your connection and try again.')} /> : null}
              {remove.isPaused ? <p role="status" className="text-base text-muted-foreground">You’re offline. Removal will resume when the connection returns.</p> : null}
              <div className="flex flex-wrap justify-end gap-3">
                <Button variant="outline" disabled={remove.isPending} onClick={scope.effect(() => setRemoving(null))}>Cancel</Button>
                <Button variant="outline" className="min-w-32 text-destructive" disabled={remove.isPending}
                  onClick={scope.effect(() => { if (removing && !remove.isPending) remove.mutate(removing.name) })}>{remove.isPending ? 'Removing...' : 'Remove server'}</Button>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </>}
    </section>
  )
}
