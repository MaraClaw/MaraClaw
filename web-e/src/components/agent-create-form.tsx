import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useEffect, useLayoutEffect, useState, type ReactNode } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { z } from 'zod'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { ValidationMessage } from '@/components/validation-message'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useTranslation } from '@/hooks/use-translation'
import type { AgentCreate } from '@/lib/engine-contracts.generated'
import { listOrgDepartments } from '@/lib/org-departments-api'
import { replaceSoulSection, soulSection } from '@/lib/soul-sections'
import { getDefaultSoul, listTemplates } from '@/lib/workspace-api'

const schema = z.object({
  name: z.string().trim().min(2, 'Use 2-100 characters for the name.').max(100, 'Use 2-100 characters for the name.'),
  template_id: z.string().optional(),
  role_description: z.string().max(500, 'Keep the role to 500 characters or fewer.').optional(),
  personality: z.string().max(64, 'Use 64 characters or fewer').optional(),
  work_style: z.string().max(64, 'Use 64 characters or fewer').optional(),
  boundaries: z.string().max(1000, 'Use 1000 characters or fewer').optional(),
  soul: z.string().max(20000, 'Keep the soul to 20000 characters or fewer.'),
  visibility: z.enum(['private', 'company', 'department']),
  department_id: z.string().optional(),
  gogcli_enabled: z.boolean(),
}).refine((values) => values.visibility !== 'department' || Boolean(values.department_id), {
  path: ['department_id'],
  message: 'Choose a department.',
})

type FormValues = z.infer<typeof schema>

export type AgentCreateFormDefaults = Partial<Pick<FormValues, 'name' | 'visibility' | 'personality' | 'work_style' | 'boundaries'>>

type AgentCreateFormProps = {
  /** Initial field values; omitted fields start empty (visibility starts Private). */
  readonly defaults?: AgentCreateFormDefaults
  /** Preselects the template with this name once templates load, unless the member already touched the field. */
  readonly defaultTemplateName?: string
  /** Already-translated submit button label. */
  readonly submitLabel: string
  /** True while the owning page's creation pipeline runs; disables submit and shows the spinner. */
  readonly pending: boolean
  /** Receives the validated `POST /api/agents/` body; the page runs `createAgent` (alone or inside a wider pipeline). */
  readonly onSubmit: (body: AgentCreate) => void
  /** Extra controls rendered beside the submit button. */
  readonly actions?: ReactNode
}

function composePersonality(values: FormValues): string {
  const parts: string[] = []
  const personality = values.personality?.trim()
  const workStyle = values.work_style?.trim()
  if (personality) parts.push(`Personality: ${personality}.`)
  if (workStyle) parts.push(`Work style: ${workStyle}.`)
  return parts.join(' ')
}

type ShortcutHeading = 'Personality' | 'Boundaries'

function shortcutBody(heading: ShortcutHeading, values: FormValues): string {
  return heading === 'Personality' ? composePersonality(values) : (values.boundaries?.trim() ?? '')
}

/** Writes a shortcut's text into its soul section; an empty shortcut restores that section from the loaded template. */
function withShortcut(soul: string, template: string, heading: ShortcutHeading, values: FormValues): string {
  const body = shortcutBody(heading, values) || soulSection(template, heading)
  return body === null ? soul : replaceSoulSection(soul, heading, body)
}

function toAgentCreate(values: FormValues, soulLoaded: boolean): AgentCreate {
  const sendSoul = soulLoaded && values.soul.trim() !== ''
  const personality = sendSoul ? '' : composePersonality(values)
  const boundaries = sendSoul ? undefined : values.boundaries?.trim()
  return {
    name: values.name,
    template_id: values.template_id || undefined,
    role_description: values.role_description,
    permission_scope_type: ({ private: 'user', company: 'company', department: 'custom' } as const)[values.visibility],
    ...(values.visibility === 'department' && values.department_id ? { department_ids: [values.department_id] } : {}),
    permission_access_level: 'use',
    gogcli_enabled: values.gogcli_enabled,
    ...(sendSoul ? { soul: values.soul } : {}),
    ...(personality ? { personality } : {}),
    ...(boundaries ? { boundaries } : {}),
  }
}

export function AgentCreateForm({ defaults, defaultTemplateName, submitLabel, pending, onSubmit, actions }: AgentCreateFormProps) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const templates = useQuery({ queryKey: ['agent-templates'], queryFn: listTemplates })
  const defaultSoul = useQuery({ queryKey: ['agent-default-soul'], queryFn: getDefaultSoul })
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      template_id: '',
      role_description: '',
      personality: '',
      work_style: '',
      boundaries: '',
      soul: '',
      visibility: 'private',
      department_id: '',
      gogcli_enabled: false,
      ...defaults,
    },
  })
  const formValues = useWatch({ control: form.control })
  const { visibility } = formValues
  const readiness = schema.safeParse(formValues)
  const departments = useQuery({
    queryKey: ['local-departments'],
    queryFn: ({ signal }) => listOrgDepartments(signal),
    enabled: visibility === 'department',
  })
  const departmentUnavailable = departments.isPending || departments.isError || departments.data?.length === 0
  const departmentError = form.formState.errors.department_id?.message
  const [retryButton, setRetryButton] = useState<HTMLButtonElement | null>(null)
  const [templateTouched, setTemplateTouched] = useState(false)
  const [soulTouched, setSoulTouched] = useState(false)
  const soulTemplate = defaultSoul.data
  const soulUnavailable = defaultSoul.isError && soulTemplate === undefined

  function applyShortcut(heading: ShortcutHeading) {
    if (soulTemplate === undefined) return
    form.setValue('soul', withShortcut(form.getValues('soul'), soulTemplate, heading, form.getValues()))
  }

  useLayoutEffect(() => {
    if (!retryButton || departments.isFetching) return
    if (scope.isCurrent() && visibility === 'department' && !departmentUnavailable && document.activeElement === retryButton) {
      form.setFocus('department_id')
    }
  }, [departmentUnavailable, departments.isFetching, form, retryButton, scope, visibility])

  useEffect(() => {
    if (!defaultTemplateName || templateTouched || !templates.data) return
    const preferred = templates.data.find((template) => template.name === defaultTemplateName)
    if (preferred) form.setValue('template_id', preferred.id)
  }, [defaultTemplateName, form, templateTouched, templates.data])

  useEffect(() => {
    if (soulTemplate === undefined || soulTouched) return
    const values = form.getValues()
    const withPersonality = withShortcut(soulTemplate, soulTemplate, 'Personality', values)
    form.setValue('soul', withShortcut(withPersonality, soulTemplate, 'Boundaries', values))
  }, [form, soulTemplate, soulTouched])

  const canCreate = readiness.success && !pending && !defaultSoul.isPending && (visibility !== 'department' || !departmentUnavailable)
  const disabledReason = pending
    ? 'Creation is in progress.'
    : defaultSoul.isPending
      ? 'Wait for the default soul to load.'
      : visibility === 'department' && departmentUnavailable
        ? departments.isPending
          ? 'Wait for departments to load.'
          : departments.isError
            ? 'Retry departments or change visibility.'
            : 'Choose another visibility to continue.'
        : readiness.error?.issues[0]?.message

  return (
    <>
      {templates.isError ? <RequestErrorToast error={templates.error} message="Unable to load agent templates. Try again." /> : null}
      {soulUnavailable ? <RequestErrorToast error={defaultSoul.error} message="Unable to load the default soul. The agent will use the standard soul." /> : null}
      <form noValidate className="space-y-4" onSubmit={form.handleSubmit((values) => {
        if (pending || (values.visibility === 'department' && departmentUnavailable)) return
        onSubmit(toAgentCreate(values, soulTemplate !== undefined))
      })}>
        <div className="space-y-2">
          <Label htmlFor="name">{translate("Name")}</Label>
          <Input id="name" {...form.register('name')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="template">{translate("Template")}</Label>
          <Select id="template" {...form.register('template_id', { onChange: () => setTemplateTouched(true) })}>
            <option value="">{translate("None")}</option>
            {(templates.data ?? []).map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="role">{translate("Role")}</Label>
          <Textarea id="role" {...form.register('role_description')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="personality">{translate("Personality")}</Label>
          <Input id="personality" {...form.register('personality', { onChange: () => applyShortcut('Personality') })} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="work-style">{translate("Work style")}</Label>
          <Input id="work-style" {...form.register('work_style', { onChange: () => applyShortcut('Personality') })} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="boundaries">{translate("Boundaries")}</Label>
          <Textarea id="boundaries" {...form.register('boundaries', { onChange: () => applyShortcut('Boundaries') })} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="soul">{translate("Soul")}</Label>
          <Textarea
            id="soul"
            rows={14}
            spellCheck={false}
            className="font-mono"
            disabled={soulTemplate === undefined}
            aria-describedby="soul-hint"
            {...form.register('soul', { onChange: () => setSoulTouched(true) })}
          />
          {soulTemplate !== undefined ? (
            <p id="soul-hint" className="text-sm text-muted-foreground">
              {translate("This becomes the agent's soul.md. Placeholders like {placeholder} are filled in when the agent is created.", { placeholder: '{{agent_name}}' })}</p>
          ) : defaultSoul.isPending ? (
            <p id="soul-hint" role="status" className="text-sm text-muted-foreground">{translate("Loading the default soul...")}</p>
          ) : (
            <p id="soul-hint" className="text-sm text-muted-foreground">{translate("Unable to load the default soul. The agent will use the standard soul.")}</p>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor="visibility">{translate("Visibility")}</Label>
          <Select id="visibility" {...form.register('visibility', { onChange: () => setRetryButton(null) })}>
            <option value="private">{translate("Private (just me)")}</option>
            <option value="company">{translate("Company-wide")}</option>
            <option value="department">{translate("Department")}</option>
          </Select>
        </div>
        {visibility === 'department' ? (
          <div className="space-y-2">
            <Label htmlFor="department">{translate("Department (required)")}</Label>
            <Select
              id="department"
              {...form.register('department_id')}
              required
              disabled={departmentUnavailable}
              aria-invalid={Boolean(departmentError)}
              aria-describedby={`department-description department-status department-admin-hint${departmentError ? ' department-error' : ''}`}
            >
              <option value="">{translate("Choose a department")}</option>
              {(departments.data ?? []).map((department) => (
                <option key={department.id} value={department.id}>{department.name}</option>
              ))}
            </Select>
            <p id="department-description" className="text-sm text-muted-foreground">
              {translate("Members of this department can use the agent. You remain its creator and can manage it.")}</p>
            <p id="department-status" role="status" className="text-sm text-muted-foreground">
              {departments.isPending
                ? translate("Loading org departments...")
                : departments.isError
                  ? ''
                  : departments.data?.length === 0
                    ? translate("No org departments yet. Ask a company admin to create one, or choose another visibility.")
                    : ''}
            </p>
            {departments.isError || retryButton ? (
              <>{departments.isError ? <RequestErrorToast error={departments.error} message="Unable to load org departments. Try again or choose another visibility." /> : null}
              <Button
                type="button"
                variant="outline"
                aria-disabled={departments.isFetching}
                aria-busy={departments.isFetching}
                onBlur={() => setRetryButton(null)}
                onClick={scope.effect((event) => {
                  if (departments.isFetching) return
                  const button = event.currentTarget
                  setRetryButton(button)
                  void departments.refetch({ cancelRefetch: false }).then(scope.effect(() => {
                    if (document.activeElement !== button) setRetryButton(null)
                  }))
                })}
              >
                {translate("Retry departments")}</Button></>
            ) : null}
            <p id="department-admin-hint" className="text-sm text-muted-foreground">{translate("Company admins manage org departments and membership.")}</p>
            {departmentError ? <p id="department-error" className="text-sm text-destructive">{departmentError}</p> : null}
          </div>
        ) : null}
        <label className="flex cursor-pointer items-start gap-3 rounded-2xl border border-border bg-card p-4 text-sm hover:bg-muted/50 has-[:checked]:border-primary/50 has-[:checked]:bg-accent has-[:focus-visible]:border-ring has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring has-[:focus-visible]:ring-offset-2 has-[:focus-visible]:ring-offset-background">
          <input
            type="checkbox"
            className="mt-0.5 size-4 shrink-0 accent-primary"
            aria-labelledby="gogcli-label"
            aria-describedby="gogcli-description"
            {...form.register('gogcli_enabled')}
          />
          <span className="min-w-0 space-y-1">
            <span id="gogcli-label" className="block font-medium">{translate("Enable Google CLI (gogcli)")}</span>
            <span id="gogcli-description" className="block text-muted-foreground">{translate("Adds Google CLI to the agent container.")}</span>
          </span>
        </label>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Button type="submit" disabled={!canCreate} aria-busy={pending} aria-describedby={disabledReason ? 'create-disabled-reason' : undefined}>
              {pending ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : null}
              {pending ? 'Creating...' : submitLabel}
            </Button>
            {actions}
          </div>
          {disabledReason ? <p id="create-disabled-reason" className="text-sm text-muted-foreground"><ValidationMessage message={disabledReason} /></p> : null}
        </div>
      </form>
    </>
  )
}
