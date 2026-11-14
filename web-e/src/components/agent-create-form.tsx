import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
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
import { useAuth } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { composeSoul, draftSoul, editSoul, fillCreationDetails, renameSoul, soulFields, type SoulDraft } from '@/lib/agent-soul'
import type { AgentCreate } from '@/lib/engine-contracts.generated'
import { listOrgDepartments } from '@/lib/org-departments-api'
import { getDefaultSoul, listTemplates } from '@/lib/workspace-api'

const schema = z.object({
  name: z.string().trim().min(2, 'Use 2-100 characters for the name.').max(100, 'Use 2-100 characters for the name.'),
  template_id: z.string().optional(),
  role_description: z.string().max(500, 'Keep the role to 500 characters or fewer.').optional(),
  soul: z.string().max(20000, 'Keep the soul to 20000 characters or fewer.'),
  visibility: z.enum(['private', 'company', 'department']),
  department_id: z.string().optional(),
}).refine((values) => values.visibility !== 'department' || Boolean(values.department_id), {
  path: ['department_id'],
  message: 'Choose a department.',
})

type FormValues = z.infer<typeof schema>

export type AgentCreateFormDefaults = Partial<Pick<FormValues, 'name' | 'visibility'>>

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

function templateStartSoul(defaultSoul: string, templateSoul: string | undefined, creatorName: string): string {
  const soul = templateSoul === undefined ? defaultSoul : composeSoul(defaultSoul, soulFields(templateSoul))
  return fillCreationDetails(soul, creatorName, new Date())
}

function toAgentCreate(values: FormValues, soulLoaded: boolean): AgentCreate {
  const sendSoul = soulLoaded && values.soul.trim() !== ''
  return {
    name: values.name,
    template_id: values.template_id || undefined,
    role_description: values.role_description,
    permission_scope_type: ({ private: 'user', company: 'company', department: 'custom' } as const)[values.visibility],
    ...(values.visibility === 'department' && values.department_id ? { department_ids: [values.department_id] } : {}),
    permission_access_level: 'use',
    gogcli_enabled: true,
    ...(sendSoul ? { soul: values.soul } : {}),
  }
}

export function AgentCreateForm({ defaults, defaultTemplateName, submitLabel, pending, onSubmit, actions }: AgentCreateFormProps) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const creatorName = useAuth().user?.display_name ?? ''
  const templates = useQuery({ queryKey: ['agent-templates'], queryFn: listTemplates })
  const defaultSoul = useQuery({ queryKey: ['agent-default-soul'], queryFn: getDefaultSoul })
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      template_id: '',
      role_description: '',
      soul: '',
      visibility: 'private',
      department_id: '',
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
  const soulSeeded = useRef(false)
  const [soulDraft, setSoulDraft] = useState<SoulDraft | null>(null)
  const soulTemplate = defaultSoul.data
  const soulUnavailable = defaultSoul.isError && soulTemplate === undefined
  const soulLoading = defaultSoul.isPending || Boolean(defaultTemplateName && templates.isPending)

  function writeSoul(draft: SoulDraft) {
    setSoulDraft(draft)
    form.setValue('soul', draft.text, { shouldValidate: form.formState.isSubmitted })
  }

  function seedSoul(base: string, templateSoul: string | undefined) {
    const role = templateSoul === undefined ? '' : soulFields(templateSoul).role
    form.setValue('role_description', role, { shouldValidate: form.formState.isSubmitted })
    writeSoul(draftSoul(templateStartSoul(base, templateSoul, creatorName), { name: form.getValues('name'), role }))
  }

  function selectTemplate(event: { target: { value: string } }) {
    if (soulTemplate === undefined) return
    const templateId = event.target.value
    seedSoul(soulTemplate, templateId ? templates.data?.find((template) => template.id === templateId)?.soul_template : undefined)
  }

  function syncSoulIdentity() {
    if (soulDraft === null) return
    writeSoul(renameSoul(soulDraft, { name: form.getValues('name'), role: form.getValues('role_description') ?? '' }))
  }

  function trackSoulEdit(event: { target: { value: string } }) {
    if (soulDraft !== null) setSoulDraft(editSoul(soulDraft, event.target.value))
  }

  useLayoutEffect(() => {
    if (!retryButton || departments.isFetching) return
    if (scope.isCurrent() && visibility === 'department' && !departmentUnavailable && document.activeElement === retryButton) {
      form.setFocus('department_id')
    }
  }, [departmentUnavailable, departments.isFetching, form, retryButton, scope, visibility])

  useEffect(() => {
    if (soulSeeded.current || soulTemplate === undefined || (defaultTemplateName && templates.isPending)) return
    soulSeeded.current = true
    const preferred = defaultTemplateName ? templates.data?.find((template) => template.name === defaultTemplateName) : undefined
    if (preferred) form.setValue('template_id', preferred.id)
    seedSoul(soulTemplate, preferred?.soul_template)
  })

  const canCreate = readiness.success && !pending && !soulLoading && (visibility !== 'department' || !departmentUnavailable)
  const disabledReason = pending
    ? 'Creation is in progress.'
    : soulLoading
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
          <Input id="name" {...form.register('name', { onChange: syncSoulIdentity })} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="template">{translate("Template")}</Label>
          <Select id="template" {...form.register('template_id', { onChange: selectTemplate })}>
            <option value="">{translate("Default")}</option>
            {(templates.data ?? []).map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="role">{translate("Role")}</Label>
          <Textarea id="role" {...form.register('role_description', { onChange: syncSoulIdentity })} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="soul">{translate("Soul")}</Label>
          <Textarea
            id="soul"
            rows={20}
            spellCheck={false}
            className="font-mono"
            disabled={soulTemplate === undefined || soulLoading}
            aria-describedby="soul-hint"
            {...form.register('soul', { onChange: trackSoulEdit })}
          />
          {soulTemplate !== undefined ? (
            <p id="soul-hint" className="text-sm text-muted-foreground">
              {translate("Name and Role update this text, and choosing a template replaces it. It becomes the agent's soul.md; placeholders like {placeholder} are filled in when the agent is created.", { placeholder: '{{agent_name}}' })}</p>
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
