import { zodResolver } from '@hookform/resolvers/zod'
import { useId, type FormEventHandler, type ReactNode } from 'react'
import { FormProvider, useForm, useFormContext } from 'react-hook-form'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { createTool, updateTool } from '@/lib/tools-api'
import { toolRecordImpact } from './tools-catalog-state'
import { createInput, mcpMetadataDefaults, metadataDefaults, metadataInput, toolCreateSchema, toolMcpMetadataSchema, toolMetadataSchema, type CatalogTool, type ToolEditorValues, type ToolMetadataValues } from './tools-forms'
import { ToolField, toolButtonClass, toolTextareaClass } from './tools-fields'
import { useToolsAction } from './use-tools-action'

export function ToolEditor({ tool, tenantId, onClose }: {
  readonly tool?: CatalogTool; readonly tenantId?: string; readonly onClose: () => void
}) {
  return tool ? <ToolMetadataEditor tool={tool} tenantId={tenantId} onClose={onClose} />
    : <ToolCreateEditor tenantId={tenantId} onClose={onClose} />
}

function ToolCreateEditor({ tenantId, onClose }: { readonly tenantId?: string; readonly onClose: () => void }) {
  const id = useId()
  const form = useForm<ToolEditorValues, unknown, z.output<typeof toolCreateSchema>>({
    resolver: zodResolver(toolCreateSchema),
    defaultValues: { ...mcpMetadataDefaults(), name: '', category: 'custom', mcp_tool_name: '' },
  })
  const mutation = useToolsAction(async (values: z.output<typeof toolCreateSchema>) => {
    if (!tenantId) throw new Error('A company is required.')
    return createTool(createInput(values, tenantId))
  }, {
    success: 'MCP tool created',
    failure: 'Could not save the tool. Check your permissions, company and tool name, then try again.', onDone: onClose,
  })
  return <FormProvider {...form}>
    <ToolDetailsForm tenantId={tenantId} onClose={onClose} onSubmit={form.handleSubmit(mutation.submit)} pending={mutation.isPending} errorMessage={mutation.errorMessage}>
      {([{ key: 'name', label: 'Tool name' }, { key: 'category', label: 'Category' }] as const).map(({ key, label }) => <ToolField key={key} id={`${id}-${key}`} label={label} error={form.formState.errors[key]?.message}>
        <Input id={`${id}-${key}`} autoComplete="off" {...form.register(key)} aria-invalid={!!form.formState.errors[key]} aria-describedby={form.formState.errors[key] ? `${id}-${key}-error` : undefined} />
      </ToolField>)}
    </ToolDetailsForm>
  </FormProvider>
}

function ToolMetadataEditor({ tool, tenantId, onClose }: {
  readonly tool: CatalogTool; readonly tenantId?: string; readonly onClose: () => void
}) {
  const schema = tool.type === 'mcp' ? toolMcpMetadataSchema : toolMetadataSchema
  const form = useForm<z.input<typeof schema>, unknown, z.output<typeof schema>>({
    resolver: zodResolver(schema),
    defaultValues: tool.type === 'mcp' ? mcpMetadataDefaults(tool) : metadataDefaults(tool),
  })
  const mutation = useToolsAction((values: z.output<typeof schema>) => updateTool(tool.id, metadataInput(values, tenantId)), {
    success: 'Tool details saved', failure: 'Could not save the tool. Check your permissions and try again.', onDone: onClose,
  })
  return <FormProvider {...form}>
    <ToolDetailsForm tool={tool} onClose={onClose} onSubmit={form.handleSubmit(mutation.submit)} pending={mutation.isPending} errorMessage={mutation.errorMessage} />
  </FormProvider>
}

function ToolConnectionFields({ creating }: { readonly creating: boolean }) {
  const id = useId()
  const { register, formState: { errors } } = useFormContext<z.input<typeof toolMcpMetadataSchema> & { readonly mcp_tool_name?: string }>()
  const fields = [
    { key: 'mcp_server_url', label: 'MCP server URL' }, { key: 'mcp_server_name', label: 'MCP server name' },
    ...(creating ? [{ key: 'mcp_tool_name', label: 'MCP tool name' }] as const : []),
  ] as const
  return fields.map(({ key, label }) => <ToolField key={key} id={`${id}-${key}`} label={label} error={errors[key]?.message}>
    <Input id={`${id}-${key}`} autoComplete="off" {...register(key)} aria-invalid={!!errors[key]} aria-describedby={errors[key] ? `${id}-${key}-error` : undefined} />
  </ToolField>)
}

function ToolDetailsForm({ tool, tenantId, onClose, onSubmit, pending, errorMessage, children }: {
  readonly tool?: CatalogTool; readonly tenantId?: string; readonly onClose: () => void
  readonly onSubmit: FormEventHandler<HTMLFormElement>; readonly pending: boolean; readonly errorMessage: string | null
  readonly children?: ReactNode
}) {
  const id = useId()
  const { register, formState: { errors } } = useFormContext<ToolMetadataValues>()
  return <Card role="region" aria-labelledby={`${id}-title`} className="min-w-0">
    <CardHeader>
      <CardTitle id={`${id}-title`} className="min-w-0 [overflow-wrap:anywhere]">{tool ? `Edit ${tool.display_name}` : 'Create MCP tool'}</CardTitle>
      <CardDescription>{tool
        ? `${toolRecordImpact(tool)} Details, Enabled and Default update this record. Existing configuration is left unchanged.`
        : 'Add one MCP tool to the selected company catalog. You can replace its configuration separately after creation.'}</CardDescription>
      {tool?.type === 'builtin' ? <p className="text-sm text-muted-foreground">This built-in tool cannot be deleted. Its configuration is managed separately in company settings.</p> : null}
      {tool ? <p className="break-words text-sm text-muted-foreground">Name: {tool.name} · Type: {tool.type} · Category: {tool.category}{tool.mcp_tool_name ? ` · MCP tool: ${tool.mcp_tool_name}` : ''}. These fields cannot be changed.</p> : null}
    </CardHeader>
    <CardContent>
      <form noValidate onSubmit={onSubmit} className="space-y-5">
        <fieldset disabled={pending} className="min-w-0 space-y-5">
          <legend className="sr-only">Tool details</legend>
          <div className="grid min-w-0 gap-5 sm:grid-cols-2">
            {children}
            <ToolField id={`${id}-display_name`} label="Display name" error={errors.display_name?.message}>
              <Input id={`${id}-display_name`} autoComplete="off" {...register('display_name')} aria-invalid={!!errors.display_name} aria-describedby={errors.display_name ? `${id}-display_name-error` : undefined} />
            </ToolField>
            {!tool || tool.type === 'mcp' ? <ToolConnectionFields creating={!tool} /> : null}
          </div>
          <ToolField id={`${id}-description`} label="Description" error={errors.description?.message}>
            <textarea id={`${id}-description`} className={toolTextareaClass} {...register('description')} aria-invalid={!!errors.description} aria-describedby={errors.description ? `${id}-description-error` : undefined} />
          </ToolField>
          <ToolField id={`${id}-schema`} label="Parameters schema (JSON object)" error={errors.parameters_schema?.message}>
            <textarea id={`${id}-schema`} className={`${toolTextareaClass} font-mono`} spellCheck={false} {...register('parameters_schema')} aria-invalid={!!errors.parameters_schema} aria-describedby={errors.parameters_schema ? `${id}-schema-error` : undefined} />
          </ToolField>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            {tool ? <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" className="size-4 accent-primary" {...register('enabled')} />Enabled</label> : null}
            <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" className="size-4 accent-primary" {...register('is_default')} />Default</label>
          </div>
        </fieldset>
        {errorMessage ? <p role="alert" className="text-sm text-destructive">{errorMessage}</p> : null}
        <div className="flex flex-wrap gap-3">
          <Button type="submit" className={toolButtonClass} disabled={pending || !tool && !tenantId}>{pending ? 'Saving…' : tool ? 'Save details' : 'Create MCP tool'}</Button>
          <Button type="button" variant="outline" className={toolButtonClass} disabled={pending} onClick={onClose}>Cancel</Button>
        </div>
      </form>
    </CardContent>
  </Card>
}
