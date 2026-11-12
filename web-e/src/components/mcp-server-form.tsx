import { useTranslation } from '@/hooks/use-translation'
import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { McpSecretFields } from '@/components/mcp-secret-fields'
import type { McpServerDraft } from '@/lib/mcp-server-draft'
import type { McpServerSummary } from '@/lib/agent-mcp-api'

export function McpServerForm({ draft, server, onChange }: {
  readonly draft: McpServerDraft
  readonly server: McpServerSummary | null
  readonly onChange: (draft: McpServerDraft) => void
}) {
  const { t: translate, formatNumber } = useTranslation()
  const editing = server !== null
  let transportFields: ReactNode
  switch (draft.transport) {
    case 'streamable-http':
    case 'sse':
      transportFields = <>
        <div className="grid gap-2">
          <Label htmlFor="mcp-server-url">{translate("Remote URL")}</Label>
          <Input id="mcp-server-url" value={draft.url} required={!editing} inputMode="url" autoComplete="off" spellCheck={false}
            onChange={(event) => onChange({ ...draft, url: event.target.value })} />
          {editing ? <p className="text-sm text-muted-foreground">{server.url_configured ? translate("A URL is stored. Leave this blank to keep it.") : translate("No URL is stored.")}</p> : null}
        </div>
        <McpSecretFields label={translate("Headers")} fields={draft.headers} onChange={(headers) => onChange({ ...draft, headers })} />
      </>
      break
    case 'stdio':
      transportFields = <>
        <div className="grid gap-2">
          <Label htmlFor="mcp-server-command">{translate("Command")}</Label>
          <Input id="mcp-server-command" value={draft.command} required={!editing} autoComplete="off" spellCheck={false}
            onChange={(event) => onChange({ ...draft, command: event.target.value })} />
          {editing ? <p className="text-sm text-muted-foreground">{server.command_configured ? translate("A command is stored. Leave this blank to keep it.") : translate("No command is stored.")}</p> : null}
        </div>
        <fieldset className="min-w-0 space-y-3">
          <legend className="text-base font-medium">{translate("Arguments")}</legend>
          {editing ? <>
          <p className="text-sm text-muted-foreground">{server.args_count === 1 ? translate("{count} saved argument. Values are hidden.", { count: formatNumber(server.args_count) }) : translate("{count} saved arguments. Values are hidden.", { count: formatNumber(server.args_count) })}</p>
            <label className="flex min-h-11 items-center gap-3 text-base font-medium" htmlFor="mcp-replace-args">
              <input id="mcp-replace-args" type="checkbox" className="size-5 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" checked={draft.replaceArgs}
                onChange={(event) => onChange({ ...draft, replaceArgs: event.target.checked })} />
              {translate("Replace arguments")}</label>
          </> : null}
          {!editing || draft.replaceArgs ? <>
          {editing ? <p className="text-sm text-muted-foreground">{translate("The replacement list overwrites all saved arguments. An empty list clears them.")}</p> : null}
          {draft.args.map((arg, index) => (
            <div key={arg.id} className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
              <div className="grid min-w-0 gap-2">
            <Label htmlFor={arg.id}>{translate("Argument")}{' '}{formatNumber(index + 1)}</Label>
                <Input id={arg.id} value={arg.value} autoComplete="off" spellCheck={false}
                  onChange={(event) => onChange({ ...draft, args: draft.args.map((item) => item.id === arg.id ? { ...item, value: event.target.value } : item) })} />
              </div>
              <Button type="button" variant="outline" aria-label={translate("Remove argument {index}", { index: index + 1 })}
                onClick={() => onChange({ ...draft, args: draft.args.filter((item) => item.id !== arg.id) })}>{translate("Remove")}</Button>
            </div>
          ))}
          <Button type="button" variant="outline" onClick={() => onChange({ ...draft, args: [...draft.args, { id: crypto.randomUUID(), value: '' }] })}>{translate("Add argument")}</Button>
          </> : null}
        </fieldset>
        <McpSecretFields label={translate("Environment variables")} fields={draft.env} onChange={(env) => onChange({ ...draft, env })} />
      </>
      break
    default:
      draft.transport satisfies never
  }
  return (
    <div className="grid min-w-0 gap-6">
      <div className="grid gap-2">
        <Label htmlFor="mcp-server-name">{translate("Server name")}</Label>
        <Input id="mcp-server-name" value={draft.name} required readOnly={editing} autoComplete="off" spellCheck={false}
          onChange={(event) => onChange({ ...draft, name: event.target.value })} />
      </div>
      <div className="grid gap-2">
        <Label htmlFor="mcp-server-transport">{translate("Transport")}</Label>
        <Select id="mcp-server-transport" value={draft.transport} disabled={editing} onChange={(event) => {
          const transport = event.target.value
          if (transport === 'streamable-http' || transport === 'sse' || transport === 'stdio') onChange({ ...draft, transport })
        }}>
          <option value="streamable-http">{translate("HTTP (Streamable HTTP)")}</option>
          <option value="sse">{translate("Server-sent events (SSE)")}</option>
          <option value="stdio">{translate("Local command (stdio)")}</option>
        </Select>
        {editing ? <p className="text-sm text-muted-foreground">{translate("To change transport, remove this server and add it again.")}</p> : null}
      </div>
      <label className="flex min-h-11 items-center gap-3 text-base font-medium" htmlFor="mcp-server-enabled">
        <input id="mcp-server-enabled" type="checkbox" className="size-5 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" checked={draft.enabled}
          onChange={(event) => onChange({ ...draft, enabled: event.target.checked })} />
        {translate("Enabled")}</label>
      {transportFields}
    </div>
  )
}
