import { zodResolver } from '@hookform/resolvers/zod'
import { useId, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { patchToolConfig, probeSavedTool, updateTool } from '@/lib/tools-api'
import { toolRecordImpact } from './tools-catalog-state'
import { configPathLabel, configurationDraft, configurationInput, configurationSchema } from './tools-config'
import { ToolField, toolButtonClass, toolTextareaClass } from './tools-fields'
import type { CatalogTool } from './tools-forms'
import { useToolsSecretAction } from './use-tools-secret-action'
import { ToolConfigOperationsEditor } from './tools-config-operations-editor'

export function ToolConfigEditor(props: {
  readonly tool: CatalogTool; readonly tenantId: string; readonly onClose: () => void
}) {
  const [replacement, setReplacement] = useState(false)
  const [probeMessage, setProbeMessage] = useState<string | null>(null)
  const action = useToolsSecretAction({ success: 'Configuration saved', failure: 'Could not save configuration. Check your permissions and try again.', onAccepted: props.onClose })
  const probe = useToolsSecretAction({ success: 'Connection test finished', failure: 'Connection test failed. Check the saved settings and draft values.', invalidate: false, onAccepted: () => setProbeMessage('Connection test passed using saved settings and the current draft.') })
  if (replacement) return <ToolConfigReplacement {...props} />
  const kind = props.tool.type === 'mcp' ? 'mcp' : props.tool.category === 'email' ? 'email' : null
  return <>
    <ToolConfigOperationsEditor tool={props.tool} pending={action.isPending || probe.isPending} error={action.errorMessage ?? probe.errorMessage}
      onSave={(operations) => { void action.submit(() => patchToolConfig(props.tool.id, { ...operations, tenant_id: props.tenantId })) }}
      onProbe={kind ? (operations) => { setProbeMessage(null); void probe.submit(async () => {
        const result = await probeSavedTool({ tool_id: props.tool.id, tenant_id: props.tenantId, kind }, operations)
        if (!result.ok) throw new Error('Connection test failed')
      }) } : undefined}
      onReplace={() => setReplacement(true)} onClose={props.onClose} />
    {probeMessage ? <p role="status" className="text-sm">{probeMessage}</p> : null}
  </>
}

function ToolConfigReplacement({ tool, tenantId, onClose }: {
  readonly tool: CatalogTool; readonly tenantId: string; readonly onClose: () => void
}) {
  const id = useId()
  const [visible, setVisible] = useState(false)
  const draft = useMemo(() => configurationDraft(tool), [tool])
  const schema = useMemo(() => configurationSchema(draft.required), [draft])
  const { register, handleSubmit, reset, formState: { errors } } = useForm<z.input<typeof schema>, unknown, z.output<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { config: draft.text, confirmed: false },
  })
  const close = () => { reset({ config: '', confirmed: false }); setVisible(false); onClose() }
  const mutation = useToolsSecretAction({
    success: 'Configuration replaced', failure: 'Could not replace configuration. Check your permissions and try again.', onAccepted: close,
  })
  return <Card role="region" aria-labelledby={`${id}-title`} className="min-w-0">
    <CardHeader>
      <CardTitle id={`${id}-title`} className="min-w-0 [overflow-wrap:anywhere]">Replace configuration: {tool.display_name}</CardTitle>
      <CardDescription>This is a full replacement, not a merge. All previous configuration values omitted from this object will be removed. Empty strings and null values are discarded by the server.</CardDescription>
      <p className="text-sm text-muted-foreground">{tool.source === 'builtin'
        ? 'This built-in source configuration is saved in the selected company’s settings. It does not change other companies’ settings.'
        : `${toolRecordImpact(tool)} Configuration is stored on this record, not as a company-specific override.`}</p>
    </CardHeader>
    <CardContent>
      <form noValidate onSubmit={handleSubmit((values) => mutation.submit(() => updateTool(tool.id, configurationInput(values.config, tenantId))))} className="space-y-5" autoComplete="off">
        <p id={`${id}-help`} className="text-sm text-muted-foreground">Only non-secret existing values are prefilled. Add all required secrets again; masked values cannot be saved. To remove a stored secret, use the API with a separately reviewed replacement.</p>
        {draft.required.length ? <div className="space-y-2 text-sm">
          <p>Re-enter existing secret keys (JSON paths):</p>
          <ul className="list-inside list-disc text-muted-foreground">{draft.required.map((path) => <li className="break-all font-mono" key={configPathLabel(path)}>{configPathLabel(path)}</li>)}</ul>
        </div> : null}
        <ToolField id={`${id}-config`} label="Complete replacement configuration (JSON object)" error={errors.config?.message}>
          <textarea id={`${id}-config`} className={`${toolTextareaClass} min-h-56 font-mono ${visible ? '' : '[-webkit-text-security:disc]'}`} autoComplete="off" autoCorrect="off" spellCheck={false} {...register('config')} disabled={mutation.isPending} aria-invalid={!!errors.config} aria-describedby={`${id}-help${errors.config ? ` ${id}-config-error` : ''}`} data-private="true" />
        </ToolField>
        <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" className="size-4 accent-primary" checked={visible} onChange={(event) => setVisible(event.target.checked)} />Show configuration JSON (may reveal secrets)</label>
        <div>
          <label className="flex min-h-11 items-start gap-3 py-3 text-sm"><input type="checkbox" className="mt-0.5 size-4 shrink-0 accent-primary" {...register('confirmed')} disabled={mutation.isPending} aria-invalid={!!errors.confirmed} aria-describedby={errors.confirmed ? `${id}-confirmed-error` : undefined} />I understand this replaces all previous configuration and removes omitted values.</label>
          {errors.confirmed ? <p id={`${id}-confirmed-error`} role="alert" className="text-sm text-destructive">{errors.confirmed.message}</p> : null}
        </div>
        {mutation.errorMessage ? <ErrorToast message={mutation.errorMessage} /> : null}
        <div className="flex flex-wrap gap-3">
          <Button type="submit" variant="destructive" className={toolButtonClass} disabled={mutation.isPending}>{mutation.isPending ? 'Replacing…' : 'Replace entire configuration'}</Button>
          <Button type="button" variant="outline" className={toolButtonClass} disabled={mutation.isPending} onClick={close}>Cancel</Button>
        </div>
      </form>
    </CardContent>
  </Card>
}
