import { useId, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import type { DiscoveredMcpTool } from '@/lib/tools-api'
import { ToolField, toolButtonClass } from './tools-fields'

export type McpConnection = { readonly server_url: string; readonly api_key?: string }
export type McpImportSelection = McpConnection & {
  readonly server_name: string; readonly name_prefix: string; readonly tools: readonly DiscoveredMcpTool[]
}

export function ToolCatalogImport({ inventory, pending, error, onDiscover, onImport, onManual, onClose }: {
  readonly inventory: readonly DiscoveredMcpTool[] | null; readonly pending: boolean; readonly error: string | null
  readonly onDiscover: (connection: McpConnection) => void
  readonly onImport: (selection: McpImportSelection) => void
  readonly onManual: () => void; readonly onClose: () => void
}) {
  const id = useId()
  const [serverUrl, setServerUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [serverName, setServerName] = useState('')
  const [prefix, setPrefix] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [confirmed, setConfirmed] = useState(false)
  const chosen = (inventory ?? []).filter((tool) => selected.has(tool.name))
  const namesValid = /^[a-zA-Z0-9_-]{1,98}$/.test(prefix) && chosen.every((tool) => `${prefix}_${tool.name}`.length <= 100)
  const connection = { server_url: serverUrl, ...(apiKey ? { api_key: apiKey } : {}) }
  const clear = () => { setApiKey(''); setSelected(new Set()); setConfirmed(false) }
  return <Card role="region" aria-labelledby={`${id}-title`} className="min-w-0">
    <CardHeader>
      <CardTitle id={`${id}-title`}>Import MCP tools</CardTitle>
      <CardDescription>Discover one HTTP MCP server, choose tools, then confirm the catalog import.</CardDescription>
    </CardHeader>
    <CardContent className="space-y-6">
      <form autoComplete="off" className="space-y-5" onSubmit={(event) => { event.preventDefault(); setSelected(new Set()); setConfirmed(false); onDiscover(connection) }}>
        <fieldset disabled={pending || inventory !== null} className="grid min-w-0 gap-5 sm:grid-cols-2">
          <ToolField id={`${id}-url`} label="MCP server URL"><Input id={`${id}-url`} type="url" required maxLength={500} value={serverUrl} onChange={(event) => setServerUrl(event.target.value)} placeholder="https://mcp.example.com/mcp" /></ToolField>
          <ToolField id={`${id}-key`} label="API key (optional)"><Input id={`${id}-key`} type="password" autoComplete="off" data-private="true" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></ToolField>
        </fieldset>
        {inventory === null ? <Button type="submit" className={toolButtonClass} disabled={pending}>{pending ? 'Discovering…' : 'Discover tools'}</Button> : null}
      </form>
      {inventory !== null ? <form className="space-y-6" onSubmit={(event) => {
        event.preventDefault()
        if (confirmed && namesValid && chosen.length) onImport({ ...connection, server_name: serverName, name_prefix: prefix, tools: chosen })
      }}>
        <fieldset disabled={pending} className="space-y-5">
          <legend className="mb-3 text-base font-medium">Choose tools ({chosen.length} of {inventory.length})</legend>
          {inventory.length === 0 ? <p className="text-sm text-muted-foreground">The server has no tools to import. Cancel or use manual creation.</p> : null}
          <ul className="divide-y divide-border">
            {inventory.map((tool) => <li key={tool.name} className="min-w-0 py-3">
              <label className="flex min-h-11 items-start gap-3 py-2">
                <input type="checkbox" className="mt-1 size-4 shrink-0 accent-primary" checked={selected.has(tool.name)} onChange={(event) => {
                  const next = new Set(selected); if (event.target.checked) next.add(tool.name); else next.delete(tool.name)
                  setSelected(next); setConfirmed(false)
                }} />
                <span className="min-w-0 text-sm"><span className="block font-medium [overflow-wrap:anywhere]">{tool.name}</span><span className="block text-muted-foreground [overflow-wrap:anywhere]">{tool.description}</span></span>
              </label>
              {selected.has(tool.name) ? <p className="break-all text-sm text-muted-foreground">Local name: {prefix}_{tool.name}</p> : null}
              <details className="text-sm"><summary className="min-h-11 cursor-pointer py-3">Parameters for {tool.name}</summary><pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-all text-sm">{JSON.stringify(tool.inputSchema, null, 2)}</pre></details>
            </li>)}
          </ul>
          <div className="grid min-w-0 gap-5 sm:grid-cols-2">
            <ToolField id={`${id}-server-name`} label="MCP server name"><Input id={`${id}-server-name`} required maxLength={98} value={serverName} onChange={(event) => { setServerName(event.target.value); setConfirmed(false) }} /></ToolField>
            <ToolField id={`${id}-prefix`} label="Local name prefix"><Input id={`${id}-prefix`} required maxLength={98} pattern="[a-zA-Z0-9_-]+" value={prefix} onChange={(event) => { setPrefix(event.target.value); setConfirmed(false) }} aria-describedby={`${id}-name-help`} /></ToolField>
          </div>
          <p id={`${id}-name-help`} className="text-sm text-muted-foreground">Names use prefix_tool-name, must be globally unique and at most 100 characters. A conflict imports no tools.</p>
          <label className="flex min-h-11 items-start gap-3 py-3 text-sm"><input type="checkbox" className="mt-1 size-4 shrink-0 accent-primary" checked={confirmed} disabled={!chosen.length || !namesValid || !serverName.trim()} onChange={(event) => setConfirmed(event.target.checked)} />I reviewed the selected definitions and local names. Import these tools into the selected company catalog.</label>
        </fieldset>
        <Button type="submit" className={toolButtonClass} disabled={pending || !confirmed || !chosen.length || !namesValid || !serverName.trim()}>{pending ? 'Importing…' : `Import ${chosen.length} selected tools`}</Button>
      </form> : null}
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
      <div className="flex flex-wrap gap-3">
        <Button type="button" variant="outline" className={toolButtonClass} disabled={pending} onClick={() => { clear(); onManual() }}>Create manually</Button>
        <Button type="button" variant="outline" className={toolButtonClass} disabled={pending} onClick={() => { clear(); onClose() }}>Cancel</Button>
      </div>
    </CardContent>
  </Card>
}
