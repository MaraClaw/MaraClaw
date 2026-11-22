import * as Dialog from '@radix-ui/react-dialog'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { agentLifecycleKey } from '@/hooks/agent-lifecycle-options'
import { agentUpdateKey } from '@/hooks/openclaw-update-options'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAgentExpiry } from '@/hooks/use-agent-expiry'
import { useAgentLifecycle } from '@/hooks/use-agent-lifecycle'
import { useAuth } from '@/hooks/use-auth'
import { useOpenClawUpdate } from '@/hooks/use-openclaw-update'
import { useTranslation } from '@/hooks/use-translation'
import { lifecycleBlockMessage, lifecycleStartBlock, type LifecycleStartBlock } from '@/lib/agent-lifecycle-policy'
import {
  canManageAgent, runtimeLossRecovery, runtimeLossSummary, startNewContainerFailureMessage, type RuntimeLossReason,
} from '@/lib/agent-runtime-loss'
import { cn } from '@/lib/utils'
import type { AgentOut } from '@/lib/workspace-api'

type RuntimeLoss = NonNullable<AgentOut['runtime_loss']>
type RecoveryGate = { readonly blocked: boolean; readonly reasons: readonly string[] }

export function AgentRuntimeLossNotice({ agent, className }: {
  readonly agent: AgentOut
  readonly className?: string
}) {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  // Each visit re-checks the agent first, so the cached copy can't reopen a loss that's already resolved.
  const [checkedAgent, setCheckedAgent] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    void scope.client.refetchQueries({ queryKey: ['agent', agent.id], exact: true })
      .finally(() => { if (live) setCheckedAgent(agent.id) })
    return () => { live = false }
  }, [scope.client, agent.id])
  const loss = agent.runtime_loss
  if (!loss || checkedAgent !== agent.id) return null
  // The lifecycle projection answers members 403, so only a manager mounts the hooks that gate recovery.
  return canManageAgent(agent, user?.id)
    ? <ManagerRuntimeLossNotice agent={agent} loss={loss} className={className} />
    : <RuntimeLossDialog agent={agent} loss={loss} className={className} gate={null} />
}

function ManagerRuntimeLossNotice({ agent, loss, className }: {
  readonly agent: AgentOut
  readonly loss: RuntimeLoss
  readonly className: string | undefined
}) {
  const { t: translate, owned } = useTranslation()
  const lifecycle = useAgentLifecycle(agent.id, true)
  const upgrading = useOpenClawUpdate(agent.id).gate === 'upgrading'
  const expired = useAgentExpiry(agent)
  const block = lifecycleStartBlock({ expired, upgrading, confirmed: lifecycle.confirmed, lifecycle: lifecycle.data })
  const reasons = (blocked: LifecycleStartBlock | null): readonly string[] => {
    switch (blocked) {
      case null: return []
      case 'expired': return [owned(lifecycleBlockMessage.expired)]
      case 'upgrading': return [owned(lifecycleBlockMessage.upgrade_in_progress)]
      case 'unconfirmed': return [!lifecycle.online || lifecycle.isPaused
        ? translate("You’re offline. Recovery will be available when you reconnect.")
        : lifecycle.isError ? translate("Unable to check whether this agent can start. It will be checked again shortly.")
          : translate("Checking whether this agent can start…")]
      case 'unavailable': {
        const codes = lifecycle.data?.availability.blocking_codes
          .filter((code) => code !== 'runtime_stopped' && code !== 'runtime_not_running') ?? []
        return codes.length ? codes.map((code) => owned(lifecycleBlockMessage[code]))
          : [translate(startNewContainerFailureMessage.start_ineligible)]
      }
    }
  }
  return <RuntimeLossDialog agent={agent} loss={loss} className={className} gate={{ blocked: block !== null, reasons: reasons(block) }} />
}

function RuntimeLossDialog({ agent, loss, className, gate }: {
  readonly agent: AgentOut
  readonly loss: RuntimeLoss
  readonly className: string | undefined
  readonly gate: RecoveryGate | null
}) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const reasonId = useId()
  // Dismissal is per recorded loss and per visit: leaving the page and coming back asks again.
  const [dismissedAt, setDismissedAt] = useState<string | null>(null)
  const start = useMutation({
    mutationFn: (reason: RuntimeLossReason) => runtimeLossRecovery[reason].start(agent.id),
    onSuccess(_agent, reason) {
      toast.success(translate(runtimeLossRecovery[reason].success))
      void scope.client.invalidateQueries({ queryKey: ['agent', agent.id] })
      void scope.client.invalidateQueries({ queryKey: agentUpdateKey(agent.id) })
      void scope.client.invalidateQueries({ queryKey: agentLifecycleKey(agent.id), exact: true })
    },
    onError() {
      void scope.client.invalidateQueries({ queryKey: agentLifecycleKey(agent.id), exact: true })
    },
  })
  const manager = gate !== null
  const recovery = runtimeLossRecovery[loss.reason]
  const open = dismissedAt !== loss.detected_at
  const busy = start.isPending

  return (
    <>
      {open ? null : (
        <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-accent px-3.5 py-2 text-sm text-accent-foreground', className)}>
          <p className="min-w-0 flex-1 font-medium [overflow-wrap:anywhere]">{translate("This agent’s workspace stopped running")}</p>
          <Button type="button" size="sm" variant="outline" className="h-auto min-h-11 whitespace-normal" onClick={() => setDismissedAt(null)}>
            {manager ? translate(recovery.label) : translate("View details")}</Button>
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
                <p>{translate(manager ? recovery.managerMessage : recovery.memberMessage)}</p>
                {gate?.reasons.length ? (
                  <div id={reasonId} className="space-y-1 text-foreground">
                    {gate.reasons.map((reason) => <p key={reason}>{reason}</p>)}
                  </div>
                ) : null}
              </div>
            </Dialog.Description>
            {start.isError ? (
              <p role="alert" className="text-sm text-destructive [overflow-wrap:anywhere]">{translate(recovery.errorMessage(start.error))}</p>
            ) : null}
            <div className="flex flex-wrap justify-end gap-2">
              <Dialog.Close asChild>
                <Button type="button" variant="outline" className="min-h-11" disabled={busy}>
                  {manager ? translate("Not now") : translate("Close")}</Button>
              </Dialog.Close>
              {gate ? (
                <Button type="button" className="h-auto min-h-11 whitespace-normal" disabled={busy || gate.blocked}
                  aria-describedby={gate.blocked && gate.reasons.length ? reasonId : undefined}
                  onClick={() => start.mutate(loss.reason)}>
                  {translate(busy ? recovery.pending : recovery.label)}</Button>
              ) : null}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  )
}
