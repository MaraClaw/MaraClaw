import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { completeOnboarding, createPersonalAssistant, markOnboardingSkipped, startOnboarding } from '@/lib/workspace-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const schema = z.object({
  name: z.string().trim().min(1).max(100),
  personality: z.string().max(64).optional(),
  work_style: z.string().max(64).optional(),
  boundaries: z.string().max(1000).optional(),
})

type FormValues = z.infer<typeof schema>

export function OnboardingPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: 'My assistant', personality: 'warm', work_style: 'concise', boundaries: '' },
  })

  const mutation = useMutation({
    mutationFn: async (values: FormValues) => {
      await startOnboarding('join')
      const created = await createPersonalAssistant(values)
      await completeOnboarding()
      return created.agent
    },
    onSuccess(agent) {
      queryClient.setQueryData(['agent', agent.id], { ...agent, access_level: 'manage' })
      toast.success(`${agent.name} is ready`)
      navigate(`/app/agents/${agent.id}/chat`, { replace: true })
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Unable to create assistant')
    },
  })

  return (
    <div className="mx-auto max-w-lg space-y-6 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Meet your assistant</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          We will create a private digital employee for you. You can add more agents later.
        </p>
      </div>
      <form className="space-y-4" onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <div className="space-y-2">
          <Label htmlFor="assistant-name">Name</Label>
          <Input id="assistant-name" {...form.register('name')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="personality">Personality</Label>
          <Input id="personality" {...form.register('personality')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="work-style">Work style</Label>
          <Input id="work-style" {...form.register('work_style')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="boundaries">Boundaries</Label>
          <Textarea id="boundaries" {...form.register('boundaries')} />
        </div>
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? <Loader2 className="size-4 animate-spin" /> : 'Create assistant'}
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              markOnboardingSkipped()
              navigate('/app/agents')
            }}
          >
            Skip for now
          </Button>
        </div>
      </form>
    </div>
  )
}
