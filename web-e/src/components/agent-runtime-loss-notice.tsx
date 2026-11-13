import * as Dialog from '@radix-ui/react-dialog'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { agentLifecycleKey } from '@/hooks/agent-lifecycle-options'
import { agentUpdateKey } from '@/hooks/openclaw-update-options'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAuth } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { canManageAgent, runtimeLossSummary, startNewContainerErrorMessage } from '@/lib/agent-runtime-loss'
import { cn } from '@/lib/utils'
import { startNewContainer, type AgentOut } from '@/lib/workspace-api'

export function AgentRuntimeLossNotice({ agent, className }: {
  readonly agent: AgentOut
  readonly className?: string
}) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  // Dismissal is per recorded loss and per visit: leaving the page and coming back asks again.
  const [dismissedAt, setDismissedAt] = useState<string | null>(null)
  // Each visit re-checks the agent first, so the cached copy can't reopen a loss that's already resolved.
  const [checkedAgent, setCheckedAgent] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    void scope.client.refetchQueries({ queryKey: ['agent', agent.id], exact: true })
      .finally(() => { if (live) setCheckedAgent(agent.id) })
    return () => { live = false }
  }, [scope.client, agent.id])
  const start = useMutation({
    mutationFn: () => startNewContainer(agent.id),
    onSuccess() {
      toast.success(translate("New container started"))
      void scope.client.invalidateQueries({ queryKey: ['agent', agent.id] })
      void scope.client.invalidateQueries({ queryKey: agentUpdateKey(agent.id) })
      void scope.client.invalidateQueries({ queryKey: agentLifecycleKey(agent.id), exact: true })
    },
    onError() {
      void scope.client.invalidateQueries({ queryKey: agentLifecycleKey(agent.id), exact: true })
    },
  })
  const loss = agent.runtime_loss
  if (!loss || checkedAgent !== agent.id) return null
  const manager = canManageAgent(agent, user?.id)
  const open = dismissedAt !== loss.detected_at
  const busy = start.isPending

  return (
    <>
      {open ? null : (
        <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-accent px-3.5 py-2 text-sm text-accent-foreground', className)}>
          <p className="min-w-0 flex-1 font-medium [overflow-wrap:anywhere]">{translate("This agent’s workspace stopped running")}</p>
          <Button type="button" size="sm" variant="outline" className="h-auto min-h-11 whitespace-normal" onClick={() => setDismissedAt(null)}>
            {manager ? translate("Start a new container") : translate("View details")}</Button>
        </div>
      )}
      <Dialog.Root open={open} onOpenChange={(next) => { if (!next && !busy) setDismissedAt(loss.detected_at) }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content role="alertdialog" aria-modal="true"
            className="fixed top-1/2 left-1/2 z-50 max-h-[90svh] w-[calc(100%_-_2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-elevated"
            onPointerDownOutside={(event) => event.preventDefault()}
            onEscapeKeyDown={(event) => { if (busy) event.preventDefault() }}>
            <Dialog.Title className="font-display text-xl font-semibold">{translate("This agent’s workspace stopped running")}</Dialog.Title>
            <Dialog.Description asChild>
              <div className="space-y-2 text-sm leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
                <p>{translate(runtimeLossSummary[loss.reason])}</p>
                <p>{manager
                  ? translate("Your conversations, memory, skills, integrations and schedules are kept. Any message you already sent will be answered once a new container starts.")
                  : translate("Ask the agent’s manager to start a new container. Your messages are saved.")}</p>
              </div>
            </Dialog.Description>
            {start.isError ? (
              <p role="alert" className="text-sm text-destructive [overflow-wrap:anywhere]">{translate(startNewContainerErrorMessage(start.error))}</p>
            ) : null}
            <div className="flex flex-wrap justify-end gap-2">
              <Dialog.Close asChild>
                <Button type="button" variant="outline" className="min-h-11" disabled={busy}>
                  {manager ? translate("Not now") : translate("Close")}</Button>
              </Dialog.Close>
              {manager ? (
                <Button type="button" className="h-auto min-h-11 whitespace-normal" disabled={busy} onClick={() => start.mutate()}>
                  {busy ? translate("Starting a new container...") : translate("Start a new container")}</Button>
              ) : null}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  )
}
