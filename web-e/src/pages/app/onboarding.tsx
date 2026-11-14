import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { assistantOnboardingOptions } from '@/hooks/onboarding-options'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { AgentCreateForm, type AgentCreateFormDefaults } from '@/components/agent-create-form'
import { Button } from '@/components/ui/button'
import { markOnboardingSkipped } from '@/lib/workspace-api'
import { userFacingRequestError } from '@/lib/http'

const assistantDefaults: AgentCreateFormDefaults = { name: 'My assistant', visibility: 'private' }

export function OnboardingPage() {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const mutation = useMutation(assistantOnboardingOptions(scope, {
    ready(agent) {
      queryClient.setQueryData(['agent', agent.id], { ...agent, access_level: 'manage' })
      toast.success(translate('{name} is ready', { name: agent.name }))
      navigate(`/app/agents/${agent.id}/chat`, { replace: true })
    },
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
      <AgentCreateForm
        defaults={assistantDefaults}
        defaultTemplateName="Private Assistant"
        submitLabel={translate("Create assistant")}
        pending={mutation.isPending}
        onSubmit={mutation.mutate}
        actions={
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
        }
      />
    </div>
  )
}
