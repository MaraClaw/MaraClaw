import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { useLayoutEffect, useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, formatApiDetail } from '@/lib/http'
import { listOrgDepartments } from '@/lib/org-departments-api'
import { createAgent, listTemplates } from '@/lib/workspace-api'

const schema = z.object({
  name: z.string().trim().min(2, 'Use 2-100 characters for the name.').max(100, 'Use 2-100 characters for the name.'),
  template_id: z.string().optional(),
  role_description: z.string().max(500, 'Keep the role to 500 characters or fewer.').optional(),
  visibility: z.enum(['private', 'company', 'department']),
  department_id: z.string().optional(),
  gogcli_enabled: z.boolean(),
}).refine((values) => values.visibility !== 'department' || Boolean(values.department_id), {
  path: ['department_id'],
  message: 'Choose a department.',
})

type FormValues = z.infer<typeof schema>

export function AgentNewPage() {
  const scope = useWorkspaceSession()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const templates = useQuery({ queryKey: ['agent-templates'], queryFn: listTemplates })
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      template_id: '',
      role_description: '',
      visibility: 'private',
      department_id: '',
      gogcli_enabled: false,
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

  useLayoutEffect(() => {
    if (!retryButton || departments.isFetching) return
    if (scope.isCurrent() && visibility === 'department' && !departmentUnavailable && document.activeElement === retryButton) {
      form.setFocus('department_id')
    }
  }, [departmentUnavailable, departments.isFetching, form, retryButton, scope, visibility])

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      createAgent({
        name: values.name,
        template_id: values.template_id || undefined,
        role_description: values.role_description,
        permission_scope_type: ({ private: 'user', company: 'company', department: 'custom' } as const)[values.visibility],
        ...(values.visibility === 'department' && values.department_id ? { department_ids: [values.department_id] } : {}),
        permission_access_level: 'use',
        gogcli_enabled: values.gogcli_enabled,
      }),
    onSuccess(agent) {
      queryClient.setQueryData(['agent', agent.id], agent)
      toast.success(`${agent.name} created`)
      navigate(`/app/agents/${agent.id}/chat`)
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Unable to create agent')
    },
  })

  const canCreate = readiness.success && !mutation.isPending && (visibility !== 'department' || !departmentUnavailable)
  const disabledReason = mutation.isPending
    ? 'Creation is in progress.'
    : visibility === 'department' && departmentUnavailable
      ? departments.isPending
        ? 'Wait for departments to load.'
        : departments.isError
          ? 'Retry departments or change visibility.'
          : 'Choose another visibility to continue.'
      : readiness.error?.issues[0]?.message

  function goBack() {
    if (!scope.isCurrent()) return
    if (typeof window !== 'undefined' && window.history.state?.idx > 0) {
      navigate(-1)
      return
    }
    navigate('/app/agents')
  }

  return (
    <div className="mx-auto max-w-lg space-y-6 p-6">
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mb-3 rounded-full px-4"
          onClick={goBack}
        >
          <ArrowLeft className="size-4" aria-hidden />
          Back
        </Button>
        <h1 className="font-display text-2xl font-semibold">New agent</h1>
        <p className="text-sm text-muted-foreground">Private by default. Share with a department or the company if teammates should use it.</p>
      </div>
      <form noValidate className="space-y-4" onSubmit={form.handleSubmit((values) => {
        if (mutation.isPending || (values.visibility === 'department' && departmentUnavailable)) return
        mutation.mutate(values)
      })}>
        <div className="space-y-2">
          <Label htmlFor="name">Name</Label>
          <Input id="name" {...form.register('name')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="template">Template</Label>
          <Select id="template" {...form.register('template_id')}>
            <option value="">None</option>
            {(templates.data ?? []).map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="role">Role</Label>
          <Textarea id="role" {...form.register('role_description')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="visibility">Visibility</Label>
          <Select id="visibility" {...form.register('visibility', { onChange: () => setRetryButton(null) })}>
            <option value="private">Private (just me)</option>
            <option value="company">Company-wide</option>
            <option value="department">Department</option>
          </Select>
        </div>
        {visibility === 'department' ? (
          <div className="space-y-2">
            <Label htmlFor="department">Department (required)</Label>
            <Select
              id="department"
              {...form.register('department_id')}
              required
              disabled={departmentUnavailable}
              aria-invalid={Boolean(departmentError)}
              aria-describedby={`department-description department-status department-admin-hint${departmentError ? ' department-error' : ''}`}
            >
              <option value="">Choose a department</option>
              {(departments.data ?? []).map((department) => (
                <option key={department.id} value={department.id}>{department.name}</option>
              ))}
            </Select>
            <p id="department-description" className="text-sm text-muted-foreground">
              Members of this department can use the agent. You remain its creator and can manage it.
            </p>
            <p id="department-status" role="status" className="text-sm text-muted-foreground">
              {departments.isPending
                ? 'Loading org departments...'
                : departments.isError
                  ? 'Unable to load org departments. Try again or choose another visibility.'
                  : departments.data?.length === 0
                    ? 'No org departments yet. Ask a company admin to create one, or choose another visibility.'
                    : ''}
            </p>
            {departments.isError || retryButton ? (
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
                Retry departments
              </Button>
            ) : null}
            <p id="department-admin-hint" className="text-sm text-muted-foreground">Company admins manage org departments and membership.</p>
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
            <span id="gogcli-label" className="block font-medium">Enable Google CLI (gogcli)</span>
            <span id="gogcli-description" className="block text-muted-foreground">Adds Google CLI to the agent container.</span>
          </span>
        </label>
        <div className="space-y-2">
          <Button type="submit" disabled={!canCreate} aria-busy={mutation.isPending} aria-describedby={disabledReason ? 'create-disabled-reason' : undefined}>
            {mutation.isPending ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : null}
            {mutation.isPending ? 'Creating...' : 'Create'}
          </Button>
          {disabledReason ? <p id="create-disabled-reason" className="text-sm text-muted-foreground">{disabledReason}</p> : null}
        </div>
      </form>
    </div>
  )
}
