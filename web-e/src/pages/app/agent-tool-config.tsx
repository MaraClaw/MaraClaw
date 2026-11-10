import { useLayoutEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { getAgentToolConfig, patchAgentToolConfig, probeAgentTool } from '@/lib/workspace-api'
import { memberConfigFields, memberFieldOperations, type FieldEdits } from '@/lib/tool-config-fields'
import { AgentToolField } from './agent-tool-field'
import { showRequestError } from '@/lib/request-error-toast'

import { RequestErrorToast } from '@/components/request-error-toast'
export function AgentToolConfigForm({ agentId, toolId, kind }: {
  readonly agentId: string; readonly toolId: string; readonly kind: 'mcp' | 'email' | null
}) {
  const scope = useWorkspaceSession()
  const active = useRef(false)
  const running = useRef(false)
  const controller = useRef<AbortController | null>(null)
  const [edits, setEdits] = useState<FieldEdits>({})
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false; controller.current?.abort() }
  }, [])
  const config = useQuery({
    queryKey: ['tool-config', agentId, toolId],
    queryFn: async ({ signal }) => {
      const response = await getAgentToolConfig(agentId, toolId, signal)
      return { ...response, fields: memberConfigFields(response.config_schema) }
    },
  })
  const isCurrent = () => active.current && scope.isCurrent()
  const execute = async (probe: boolean) => {
    if (!isCurrent() || running.current || !config.data) return
    running.current = true
    setPending(true)
    setMessage(null)
    setError(null)
    try {
      const operations = memberFieldOperations(config.data.fields, edits)
      if (probe && kind) {
        const owned = new AbortController()
        controller.current = owned
        const result = await probeAgentTool({ agentId, toolId, kind }, operations, owned.signal)
        if (!isCurrent()) return
        if (result.ok) setMessage('Connection test passed using saved settings and the current draft.')
        else setError('Connection test failed. Check saved settings and draft values.')
      } else {
        await patchAgentToolConfig(agentId, toolId, operations)
        if (!scope.isCurrent()) return
        await scope.client.invalidateQueries({ queryKey: ['tool-config', agentId, toolId] })
        await scope.client.invalidateQueries({ queryKey: ['tools', agentId] })
        if (isCurrent()) { setEdits({}); setMessage('Configuration saved. Unchanged values were preserved.') }
      }
    } catch (cause) {
      if (!(cause instanceof Error)) throw cause
      if (isCurrent()) showRequestError(probe ? 'Unable to test the connection. Try again.' : 'Could not save. Enter new values for fields marked Set; check your permissions and try again.', cause)
    } finally {
      running.current = false
      controller.current = null
      if (isCurrent()) setPending(false)
    }
  }
  if (config.isPending) return <p role="status" className="mt-3 text-sm">Loading configuration…</p>
  if (config.isError) return <div className="mt-3 space-y-3"><RequestErrorToast error={config.error} message="Could not load configuration." /><Button type="button" variant="outline" onClick={() => { void config.refetch() }}>Retry configuration</Button></div>
  const changed = Object.values(edits).some((edit) => edit.operation !== 'preserve')
  return <form autoComplete="off" noValidate className="mt-4 space-y-6 border-t border-border pt-4" onSubmit={(event) => { event.preventDefault(); void execute(false) }}>
    <p className="text-sm text-muted-foreground">Only explicit edits change agent overrides. Removing an override restores inheritance. Secrets are never prefilled.</p>
    <div className="grid min-w-0 gap-6 sm:grid-cols-2">
      {config.data.fields.map((field) => {
        const secret = field.type === 'password' || ['api_key', 'auth_code', 'password', 'secret', 'private_key'].includes(field.key)
        const globalValue = config.data.global_config[field.key]
        const agentValue = config.data.agent_config[field.key]
        return <AgentToolField key={field.key} field={field} edit={edits[field.key]} overridden={agentValue !== undefined} inherited={globalValue !== undefined}
          inheritedDisplay={globalValue === undefined ? 'None' : secret ? 'Saved secret' : String(globalValue)} overrideDisplay={agentValue === undefined ? 'None' : secret ? 'Saved secret' : String(agentValue)}
          disabled={pending} onChange={scope.effect((edit) => { setMessage(null); setEdits((previous) => ({ ...previous, [field.key]: edit })) })} />
      })}
    </div>
    {!config.data.fields.length ? <p className="text-sm text-muted-foreground">No editable fields. Network and proxy settings are managed by administrators.</p> : null}
    {message ? <p role="status" className="text-sm">{message}</p> : null}
    {error ? <p role="status" className="text-sm text-destructive">{error}</p> : null}
    <div className="flex flex-wrap gap-3">
      <Button type="submit" disabled={pending || !changed}>{pending ? 'Working…' : 'Save config'}</Button>
      {kind ? <Button type="button" variant="outline" disabled={pending} onClick={() => { void execute(true) }}>Test saved + draft</Button> : null}
      <Button type="button" variant="outline" disabled={pending} onClick={() => { setEdits({}); setError(null); setMessage(null) }}>Discard draft</Button>
    </div>
  </form>
}
