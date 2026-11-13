import * as Dialog from '@radix-ui/react-dialog'
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { agentDailyMessagesResetOptions } from '@/hooks/agent-daily-messages-options'
import { useAuth } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import { agentDailyMessagesError, canResetAgentDailyMessages, isAgentDailyLimitReached } from '@/lib/agent-daily-messages'
import { cn } from '@/lib/utils'
import type { AdminUser, AgentDailyMessages, UserAgent } from '@/lib/users-api'

function CappedAgentMessages({ owner, agent, daily }: {
  readonly owner: Pick<AdminUser, 'id' | 'tenant_id'>
  readonly agent: UserAgent
  readonly daily: AgentDailyMessages
}) {
  const { t, owned, number, date } = useTranslation()
  const { user, mustChangePassword } = useAuth()
  const [open, setOpen] = useState(false)
  const resetCount = useSessionMutation(agentDailyMessagesResetOptions(useQueryClient(), owner.id, agent.id))
  const canReset = canResetAgentDailyMessages(user, owner) && !mustChangePassword
  const atLimit = isAgentDailyLimitReached(daily)

  function onOpenChange(next: boolean) {
    if (resetCount.isPending) return
    if (next) resetCount.reset()
    setOpen(next)
  }

  function confirm() {
    if (resetCount.isPending) return
    resetCount.mutate()
  }

  return <div className="flex w-full flex-wrap items-start justify-between gap-3">
    <div className="min-w-0">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium tabular-nums">{t('{used} / {limit} messages today', { used: number(daily.used), limit: number(daily.limit) })}</span>
        {atLimit ? <Badge variant="warning">{t('Daily limit reached')}</Badge> : null}
      </p>
      <p className="mt-0.5 text-xs text-muted-foreground">{t('Resets {time} ({timezone})', {
        time: date(daily.resets_at, { timeZone: daily.timezone, dateStyle: 'medium', timeStyle: 'short' }),
        timezone: daily.timezone,
      })}</p>
    </div>
    {canReset ? <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Trigger asChild>
        <Button type="button" variant="outline" disabled={!atLimit} aria-label={t('Reset daily messages for {name}', { name: agent.name })}>{t('Reset daily messages')}</Button>
      </Dialog.Trigger>
      <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm" />
        <Dialog.Content className="fixed start-1/2 top-1/2 z-50 flex max-h-[90svh] w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 flex-col gap-5 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-card [overflow-wrap:anywhere]">
          <Dialog.Title className="font-display text-lg font-semibold">{t('Reset daily messages')}</Dialog.Title>
          <div className="flex flex-col">
            <Dialog.Description className="text-sm leading-relaxed text-muted-foreground">
              {t('Reset today’s count for {agent}? It starts again from 0 now and resets automatically at midnight ({timezone}).', { agent: agent.name, timezone: daily.timezone })}
            </Dialog.Description>
            <p role="alert" className={cn('text-sm text-destructive', resetCount.isError && 'mt-3')}>
              {resetCount.isError ? owned(agentDailyMessagesError(resetCount.error)) : ''}
            </p>
            <p role="status" className={cn('text-sm font-medium', resetCount.isSuccess && 'mt-3')}>
              {resetCount.isPending ? <span className="sr-only">{t('Resetting...')}</span> : resetCount.isSuccess ? t('Daily messages reset for {name}.', { name: agent.name }) : ''}
            </p>
          </div>
          {resetCount.isError ? <ErrorToast error={resetCount.error} message={owned(agentDailyMessagesError(resetCount.error))} /> : null}
          <div className="flex flex-wrap gap-3">
            {resetCount.isSuccess ? null : <Button type="button" disabled={resetCount.isPending} onClick={confirm}>{resetCount.isPending ? t('Resetting...') : t('Reset daily messages')}</Button>}
            <Dialog.Close asChild>
              <Button key={resetCount.isSuccess ? 'close' : 'cancel'} type="button" variant="outline" autoFocus={resetCount.isSuccess} disabled={resetCount.isPending}>{resetCount.isSuccess ? t('Close') : t('Cancel')}</Button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root> : null}
  </div>
}

export function AgentDailyMessages({ owner, agent }: {
  readonly owner: Pick<AdminUser, 'id' | 'tenant_id'>
  readonly agent: UserAgent
}) {
  const daily = agent.daily_messages
  return daily ? <CappedAgentMessages owner={owner} agent={agent} daily={daily} /> : null
}
