import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { AgentCreateForm } from '@/components/agent-create-form'
import { BackButton } from '@/components/ui/back-button'
import { userFacingRequestError } from '@/lib/http'
import { createAgent } from '@/lib/workspace-api'

export function AgentNewPage() {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: createAgent,
    onSuccess(agent) {
      queryClient.setQueryData(['agent', agent.id], agent)
      toast.success(translate('{name} created', { name: agent.name }))
      navigate(`/app/agents/${agent.id}/chat`)
    },
    onError(error) {
      toast.error(userFacingRequestError(error, translate("Unable to create agent")))
    },
  })

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
        <BackButton className="mb-3" onClick={goBack} label={translate("Back")} />
        <h1 className="font-display text-2xl font-semibold">{translate("New agent")}</h1>
        <p className="text-sm text-muted-foreground">{translate("Private by default. Share with a department or the company if teammates should use it.")}</p>
      </div>
      <AgentCreateForm submitLabel={translate("Create")} pending={mutation.isPending} onSubmit={mutation.mutate} />
    </div>
  )
}
