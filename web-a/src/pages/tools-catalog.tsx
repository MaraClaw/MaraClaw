import { useQuery } from '@tanstack/react-query'
import { Plus, RefreshCw, Wrench } from 'lucide-react'
import { useId, useLayoutEffect, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { listTools } from '@/lib/tools-api'
import { toolActions, toolOwnership } from './tools-catalog-state'
import { ToolConfigEditor } from './tools-config-editor'
import { ToolDeleteConfirmation } from './tools-delete'
import { ToolEditor } from './tools-editor'
import { toolButtonClass } from './tools-fields'
import { toolsQueryKey, type CatalogTool } from './tools-forms'

type Editor = { readonly kind: 'create' }
  | { readonly kind: 'edit' | 'config' | 'delete'; readonly tool: CatalogTool }

export function ToolsCatalog({ tenantId, concreteCompany }: {
  readonly tenantId?: string; readonly concreteCompany: boolean
}) {
  const id = useId()
  const catalog = useQuery({ queryKey: toolsQueryKey(tenantId), queryFn: ({ signal }) => listTools(tenantId, signal) })
  const [editor, setEditor] = useState<Editor | null>(null)
  const panel = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const origin = useRef<HTMLButtonElement | null>(null)
  useLayoutEffect(() => { if (editor) panel.current?.focus() }, [editor])
  const open = (next: Editor, trigger: HTMLButtonElement) => { origin.current = trigger; setEditor(next) }
  const close = () => { setEditor(null); (origin.current?.isConnected ? origin.current : heading.current)?.focus() }
  const finishDelete = () => { setEditor(null); heading.current?.focus() }
  const renderEditor = () => {
    if (!editor) return null
    switch (editor.kind) {
      case 'create': return concreteCompany && tenantId ? <ToolEditor tenantId={tenantId} onClose={close} /> : null
      case 'edit': return <ToolEditor tool={editor.tool} tenantId={tenantId} onClose={close} />
      case 'config': return concreteCompany && tenantId ? <ToolConfigEditor tool={editor.tool} tenantId={tenantId} onClose={close} /> : null
      case 'delete': return <ToolDeleteConfirmation tool={editor.tool} onClose={close} onDeleted={finishDelete} />
    }
  }
  return <section aria-labelledby={`${id}-heading`} className="flex min-w-0 flex-col gap-5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 ref={heading} tabIndex={-1} id={`${id}-heading`} className="font-display text-lg font-semibold tracking-tight outline-none">Tool catalog</h2>
        {catalog.data ? <p className="text-sm text-muted-foreground">{catalog.data.length} {catalog.data.length === 1 ? 'tool' : 'tools'} available in this catalog context</p> : null}
      </div>
      <div className="flex flex-wrap gap-3">
        <Button variant="outline" className={toolButtonClass} disabled={catalog.isFetching} onClick={() => { void catalog.refetch() }}><RefreshCw aria-hidden className="size-4" />Refresh catalog</Button>
        <Button className={toolButtonClass} disabled={!concreteCompany} aria-describedby={!concreteCompany ? 'tools-company-help' : undefined} onClick={(event) => open({ kind: 'create' }, event.currentTarget)}><Plus aria-hidden className="size-4" />Create MCP tool</Button>
      </div>
    </div>
    {editor ? <div ref={panel} tabIndex={-1} className="min-w-0 outline-none" key={editor.kind === 'create' ? 'create' : `${editor.kind}-${editor.tool.id}`}>{renderEditor()}</div> : null}
    {catalog.isPending ? <p role="status" className="py-8 text-sm text-muted-foreground">Loading tools…</p> : null}
    {catalog.isError ? <Card><CardContent className="space-y-4">
      <p role="alert" className="text-sm text-destructive">Could not load the tool catalog. Check your company selection and permissions, then retry.</p>
      <Button variant="outline" className={toolButtonClass} disabled={catalog.isFetching} onClick={() => { void catalog.refetch() }}>Retry loading tools</Button>
    </CardContent></Card> : null}
    {catalog.isSuccess && catalog.data.length === 0 ? <Card><CardContent className="space-y-2 py-4 text-center">
      <Wrench className="mx-auto size-6 text-muted-foreground" aria-hidden />
      <h3 className="font-display font-semibold">No tools in this catalog</h3>
      <p className="text-sm text-muted-foreground">{concreteCompany ? 'Create an MCP tool to get started.' : 'Choose a company to create an MCP tool.'}</p>
    </CardContent></Card> : null}
    <div className="grid min-w-0 gap-4 lg:grid-cols-2">
      {catalog.data?.map((tool) => {
        const actions = toolActions(tool, concreteCompany)
        const helpId = `${id}-${tool.id}`
        return <Card key={tool.id} className="min-w-0">
        <CardHeader>
          <CardTitle className="break-words">{tool.display_name}</CardTitle>
          <p className="break-all font-mono text-xs text-muted-foreground">{tool.name}</p>
          <div className="flex flex-wrap gap-2 pt-2">
            <Badge variant={tool.enabled ? 'success' : 'secondary'}>{tool.enabled ? 'Enabled' : 'Disabled'}</Badge>
            <Badge variant="outline">{tool.is_default ? 'Default' : 'Not default'}</Badge>
          </div>
        </CardHeader>
        <CardContent className="flex flex-1 flex-col gap-4">
          {tool.description ? <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{tool.description}</p> : null}
          <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Type</dt><dd className="break-all">{tool.type}</dd>
            <dt className="text-muted-foreground">Category</dt><dd className="break-all">{tool.category}</dd>
            <dt className="text-muted-foreground">Source</dt><dd className="break-all">{tool.source}</dd>
            <dt className="text-muted-foreground">Ownership</dt><dd className="break-all">{toolOwnership(tool)}</dd>
            {tool.tenant_id !== null ? <><dt className="text-muted-foreground">Company ID</dt><dd className="break-all font-mono text-xs">{tool.tenant_id}</dd></> : null}
            {tool.mcp_server_name ? <><dt className="text-muted-foreground">Server</dt><dd className="break-all">{tool.mcp_server_name}</dd></> : null}
            {tool.mcp_server_url ? <><dt className="text-muted-foreground">Server URL</dt><dd className="break-all">{tool.mcp_server_url}</dd></> : null}
            {tool.mcp_tool_name ? <><dt className="text-muted-foreground">MCP tool</dt><dd className="break-all">{tool.mcp_tool_name}</dd></> : null}
          </dl>
          <div className="mt-auto flex flex-wrap gap-2 pt-2">
            <Button variant="outline" className={toolButtonClass} disabled={!actions.editDetails} aria-describedby={!actions.editDetails ? `${helpId}-edit-help` : undefined} aria-label={`Edit details for ${tool.display_name}`} onClick={(event) => open({ kind: 'edit', tool }, event.currentTarget)}>Edit details</Button>
            <Button variant="outline" className={toolButtonClass} disabled={!actions.configure} aria-describedby={!actions.configure ? `${helpId}-config-help` : undefined} aria-label={`Replace configuration for ${tool.display_name}`} onClick={(event) => open({ kind: 'config', tool }, event.currentTarget)}>Replace configuration</Button>
            {tool.type !== 'builtin' ? <Button variant="ghost" className={`${toolButtonClass} text-destructive`} disabled={!actions.delete} aria-describedby={!actions.delete ? `${helpId}-delete-help` : undefined} aria-label={`Delete tool ${tool.display_name}`} onClick={(event) => open({ kind: 'delete', tool }, event.currentTarget)}>Delete tool</Button> : null}
          </div>
          {!actions.editDetails || !actions.configure || tool.type !== 'builtin' && !actions.delete ? <div className="space-y-1 text-sm text-muted-foreground">
            {!actions.editDetails ? <p id={`${helpId}-edit-help`}>Your permissions do not allow editing details for this record.</p> : null}
            {!actions.configure ? <p id={`${helpId}-config-help`}>{tool.capabilities.can_configure ? 'Select a concrete company before replacing configuration.' : 'Your permissions do not allow configuring this record in this company context.'}</p> : null}
            {tool.type !== 'builtin' && !actions.delete ? <p id={`${helpId}-delete-help`}>Your permissions do not allow deleting this record.</p> : null}
          </div> : null}
        </CardContent>
      </Card>})}
    </div>
  </section>
}
