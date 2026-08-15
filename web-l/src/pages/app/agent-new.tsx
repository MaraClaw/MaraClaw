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

const schema = z.object({
  name: z.string().trim().min(2).max(100),
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
    defaultValues: { name: '', template_id: '', role_description: '', visibility: 'private', gogcli_enabled: false },
  })

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      createAgent({
        name: values.name,
        template_id: values.template_id || undefined,
        role_description: values.role_description,
        permission_scope_type: values.visibility === 'private' ? 'user' : 'company',
        permission_access_level: 'use',
        agent_type: 'native',
        gogcli_enabled: values.gogcli_enabled,
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
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" {...form.register('gogcli_enabled')} />
          Enable gogcli (Google CLI in the agent container)
        </label>
        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? <Loader2 className="size-4 animate-spin" /> : 'Create'}
        </Button>
      </form>
    </div>
  )
}
