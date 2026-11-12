import { useTranslation } from '@/hooks/use-translation'
import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { assistantOnboardingOptions } from '@/hooks/onboarding-options'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { markOnboardingSkipped } from '@/lib/workspace-api'
import type { AssistantStartFailure } from '@/lib/onboarding-api'
import { formatApiDetail, userFacingRequestError } from '@/lib/http'

const schema = z.object({
  name: z.string().trim().min(1, 'Enter a name').max(100, 'Use 100 characters or fewer'),
  personality: z.string().max(64, 'Use 64 characters or fewer').optional(),
  work_style: z.string().max(64, 'Use 64 characters or fewer').optional(),
  boundaries: z.string().max(1000, 'Use 1000 characters or fewer').optional(),
})

type FormValues = z.infer<typeof schema>

export function OnboardingPage() {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [startFailure, setStartFailure] = useState<AssistantStartFailure | null>(null)
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: 'My assistant', personality: 'warm', work_style: 'concise', boundaries: '' },
  })

  const mutation = useMutation(assistantOnboardingOptions(scope, {
    ready(agent) {
      queryClient.setQueryData(['agent', agent.id], { ...agent, access_level: 'manage' })
      toast.success(translate('{name} is ready', { name: agent.name }))
      navigate(`/app/agents/${agent.id}/chat`, { replace: true })
    },
    startFailed: setStartFailure,
    error(error) {
      toast.error(userFacingRequestError(error, translate("Unable to create assistant")))
    },
  }))

  return (
    <div className="mx-auto max-w-lg space-y-6 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">{translate("Meet your assistant")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {translate("We will create a private digital employee for you. You can add more agents later.")}</p>
      </div>
      <form className="space-y-4" onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <div className="space-y-2">
          <Label htmlFor="assistant-name">{translate("Name")}</Label>
          <Input id="assistant-name" readOnly={startFailure !== null} {...form.register('name')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="personality">{translate("Personality")}</Label>
          <Input id="personality" readOnly={startFailure !== null} {...form.register('personality')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="work-style">{translate("Work style")}</Label>
          <Input id="work-style" readOnly={startFailure !== null} {...form.register('work_style')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="boundaries">{translate("Boundaries")}</Label>
          <Textarea id="boundaries" readOnly={startFailure !== null} {...form.register('boundaries')} />
        </div>
        {startFailure && (
          <p role="alert" className="text-sm text-destructive">
            {translate("Your assistant was saved, but could not start.")}{' '}{formatApiDetail(startFailure.message)} {translate("Retry starting the same assistant.")}</p>
        )}
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending && <Loader2 aria-hidden="true" className="size-4 animate-spin" />}
            {startFailure ? translate("Retry start") : translate("Create assistant")}
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={mutation.isPending}
            onClick={() => {
              if (!scope.isCurrent()) return
              markOnboardingSkipped()
              navigate('/app/agents')
            }}
          >
            {translate("Skip for now")}</Button>
        </div>
      </form>
    </div>
  )
}
