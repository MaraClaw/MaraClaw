import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, formatApiDetail } from '@/lib/http'
import { createAgent, listTemplates } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const agentTypes = [
  {
    value: 'openclaw',
    label: 'OpenClaw',
    description: 'Runs in its own environment and picks up work from MaraClaw.',
  },
  {
    value: 'native',
    label: 'Native',
    description: 'Replies in this workspace. Chat, files, and tools stay on the platform.',
  },
] as const

const schema = z.object({
  name: z.string().trim().min(2).max(100),
  agent_type: z.enum(['native', 'openclaw']),
  template_id: z.string().optional(),
  role_description: z.string().max(500).optional(),
  visibility: z.enum(['private', 'company']),
  gogcli_enabled: z.boolean(),
})

type FormValues = z.infer<typeof schema>

export function AgentNewPage() {
  const navigate = useNavigate()
  const templates = useQuery({ queryKey: ['agent-templates'], queryFn: listTemplates })
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      agent_type: 'openclaw',
      template_id: '',
      role_description: '',
      visibility: 'private',
      gogcli_enabled: false,
    },
  })
  const agentType = form.watch('agent_type')

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      createAgent({
        name: values.name,
        template_id: values.template_id || undefined,
        role_description: values.role_description,
        permission_scope_type: values.visibility === 'private' ? 'user' : 'company',
        permission_access_level: 'use',
        agent_type: values.agent_type,
        gogcli_enabled: values.agent_type === 'openclaw' && values.gogcli_enabled,
      }),
    onSuccess(agent) {
      toast.success(`${agent.name} created`)
      navigate(`/app/agents/${agent.id}/chat`)
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Unable to create agent')
    },
  })

  return (
    <div className="mx-auto max-w-lg space-y-6 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">New agent</h1>
        <p className="text-sm text-muted-foreground">Private by default. Share with the company if teammates should use it.</p>
      </div>
      <form className="space-y-4" onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <div className="space-y-2">
          <Label htmlFor="name">Name</Label>
          <Input id="name" {...form.register('name')} />
        </div>
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium leading-none text-foreground">Type</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {agentTypes.map((option) => {
              const selected = agentType === option.value
              return (
                <label
                  key={option.value}
                  className={cn(
                    'flex cursor-pointer flex-col gap-1 rounded-xl border px-3 py-3 text-sm transition-colors',
                    selected ? 'border-primary bg-primary/5' : 'border-input hover:border-primary/40',
                  )}
                >
                  <span className="flex items-center gap-2 font-medium">
                    <input type="radio" value={option.value} className="size-4 accent-primary" {...form.register('agent_type')} />
                    {option.label}
                  </span>
                  <span className="leading-relaxed text-muted-foreground">{option.description}</span>
                </label>
              )
            })}
          </div>
        </fieldset>
        <div className="space-y-2">
          <Label htmlFor="template">Template</Label>
          <select
            id="template"
            className="h-11 w-full rounded-xl border border-input bg-transparent px-3 text-sm"
            {...form.register('template_id')}
          >
            <option value="">None</option>
            {(templates.data ?? []).map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="role">Role</Label>
          <Textarea id="role" {...form.register('role_description')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="visibility">Visibility</Label>
          <select
            id="visibility"
            className="h-11 w-full rounded-xl border border-input bg-transparent px-3 text-sm"
            {...form.register('visibility')}
          >
            <option value="private">Private (just me)</option>
            <option value="company">Company-wide</option>
          </select>
        </div>
        {agentType === 'openclaw' ? (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" {...form.register('gogcli_enabled')} />
            Enable Google CLI (gogcli) in the agent container
          </label>
        ) : null}
        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? <Loader2 className="size-4 animate-spin" /> : 'Create'}
        </Button>
      </form>
    </div>
  )
}
