import * as Dialog from '@radix-ui/react-dialog'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { agentTransferOptions, transferSourceOptions, transferTargetsOptions } from '@/hooks/agent-transfer-options'
import { useAuth } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import { agentLifecycleError } from '@/lib/agent-lifecycle-errors'
import { agentTransferSchema, canTransferMemberAgents, transferableAgents, type TransferScope } from '@/lib/agent-transfers-api'
import { agentStatusLabel, type AdminUser } from '@/lib/users-api'

function TransferForm({ scope, sourceName }: { readonly scope: TransferScope; readonly sourceName: string }) {
  const { t, owned } = useTranslation()
  const recipientId = useId()
  const submitting = useRef(false)
  const [recipient, setRecipient] = useState<AdminUser | null>(null)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [selected, setSelected] = useState<readonly string[]>([])
  const [validation, setValidation] = useState('')
  const source = useQuery(transferSourceOptions(scope.sourceUserId, true))
  const targets = useInfiniteQuery(transferTargetsOptions(scope, debouncedSearch, true))
  const members = targets.data?.pages.flatMap((page) => page.items) ?? []
  const options = recipient && !members.some((member) => member.id === recipient.id) ? [recipient, ...members] : members
  const transfer = useSessionMutation(agentTransferOptions(useQueryClient()))
  const { user } = useAuth()
  const sourceEligible = source.data && source.data.id === scope.sourceUserId && canTransferMemberAgents(user, source.data)
  const agents = sourceEligible ? transferableAgents(source.data) : []
  const ready = source.isSuccess && !source.isFetching && sourceEligible && !transfer.isPending && !transfer.isSuccess

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search), 300)
    return () => window.clearTimeout(timeout)
  }, [search])

  function submit() {
    if (!ready || submitting.current) return
    const parsed = agentTransferSchema.safeParse({ from_user_id: scope.sourceUserId, to_user_id: recipient?.id, agent_ids: selected })
    if (!parsed.success || !recipient || selected.some((id) => !agents.some((agent) => agent.id === id))) {
      setValidation(t('Choose an active recipient in this organization and between 1 and 100 available agents.'))
      return
    }
    setValidation('')
    submitting.current = true
    transfer.mutate(parsed.data, { onSettled: () => { submitting.current = false } })
  }

  return <div className="flex flex-col gap-5">
    <Dialog.Description className="text-sm leading-relaxed text-muted-foreground">{t('Transfer agents owned by {sourceName} to an active member in this organization. This does not activate or deactivate either member. Pending deletion requests for transferred agents will be cancelled.', { sourceName })}</Dialog.Description>
    {source.isPending ? <p role="status" className="text-sm">{t('Loading agents...')}</p> : null}
    {source.isError ? <div className="flex flex-col items-start gap-3">
      <ErrorToast error={source.error} message={t('Unable to load transfer options. Check your connection and try again.')} />
      <Button variant="outline" disabled={source.isFetching} onClick={() => { setSelected([]); void source.refetch() }}>{t('Reload transfer options')}</Button>
    </div> : null}
    {source.isSuccess && !sourceEligible ? <p role="alert" className="text-sm">{t('This member is no longer in your organization or is not eligible. Close this dialog and refresh Users.')}</p> : null}
    {sourceEligible ? <>
      <fieldset disabled={!ready} className="flex min-w-0 flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">{t('Owned non-system agents ({selected} / 100 selected)', { selected: selected.length })}</legend>
        {agents.length === 0 ? <p className="text-sm text-muted-foreground">{t('No transferable agents. Close this dialog to choose another member.')}</p> : <div className="max-h-64 overflow-y-auto rounded-xl border border-border p-3">
          {agents.map((agent) => <label key={agent.id} className="flex min-h-11 items-start gap-3 py-2 text-sm">
            <input type="checkbox" name="agent_ids" value={agent.id} className="mt-0.5 size-5 shrink-0 accent-primary" checked={selected.includes(agent.id)}
              disabled={!selected.includes(agent.id) && selected.length >= 100}
              onChange={(event) => { setSelected((values) => event.target.checked ? [...values, agent.id] : values.filter((id) => id !== agent.id)); setValidation('') }} />
            <span className="min-w-0"><span className="font-medium">{agent.name}</span><span className="mt-1 block text-xs text-muted-foreground">{owned(agentStatusLabel(agent.status, agent.is_expired))}</span></span>
          </label>)}
        </div>}
      </fieldset>
      <div className="flex flex-col gap-2">
        <label htmlFor={`${recipientId}-search`} className="text-sm font-medium">{t('Search recipients')}</label>
        <Input id={`${recipientId}-search`} type="search" value={search} disabled={transfer.isPending || transfer.isSuccess} placeholder={t('Name or email')} onChange={(event) => setSearch(event.target.value)} />
        <label htmlFor={recipientId} className="text-sm font-medium">{t('Transfer to')}</label>
        <Select id={recipientId} name="to_user_id" value={recipient?.id ?? ''} disabled={!ready} onChange={(event) => { setRecipient(options.find((member) => member.id === event.target.value) ?? null); setValidation('') }} aria-describedby={`${recipientId}-hint`}>
          <option value="">{t('Choose a member')}</option>
          {options.map((member) => <option key={member.id} value={member.id}>{member.display_name ?? member.email ?? member.id}{member.email && member.display_name ? ` (${member.email})` : ''}</option>)}
        </Select>
        <p id={`${recipientId}-hint`} className="text-xs text-muted-foreground">{t('Active members and agent admins only. Organization admins and members of other organizations are excluded.')}</p>
        {targets.isFetching || search !== debouncedSearch ? <p role="status" className="text-sm">{t('Loading recipients...')}</p> : null}
        {targets.isError ? <ErrorToast error={targets.error} message={t('Unable to load recipients. Check your connection and try again.')} /> : null}
        {targets.isError && !targets.isFetchNextPageError ? <Button type="button" variant="outline" disabled={targets.isFetching} onClick={() => { void targets.refetch() }}>{t('Reload recipients')}</Button> : null}
        {targets.hasNextPage ? <Button type="button" variant="outline" disabled={targets.isFetching || search !== debouncedSearch || transfer.isPending || transfer.isSuccess} onClick={() => { void targets.fetchNextPage() }}>{t('Load more recipients')}</Button> : null}
        {targets.isSuccess && members.length === 0 ? <p className="text-sm text-muted-foreground">{debouncedSearch ? t('No matching recipients. Try another name or email.') : t('No eligible recipients. Activate or add another member before transferring agents.')}</p> : null}
      </div>
    </> : null}
    <p role="alert" className="text-sm text-destructive">{validation}</p>
    {transfer.isError ? <ErrorToast error={transfer.error} message={owned(agentLifecycleError(transfer.error))} /> : null}
    <p role="status" className="text-sm">{transfer.isSuccess ? t('{transferred} agents transferred. {cancelled} pending deletion requests cancelled.', { transferred: transfer.data.transferred.length, cancelled: transfer.data.cancelled_deletion_request_ids.length }) : ''}</p>
    <div className="flex flex-wrap gap-3">
      {transfer.isSuccess ? null : <Button type="button" disabled={!ready || agents.length === 0 || !recipient || selected.length === 0} onClick={submit}>{transfer.isPending ? t('Transferring...') : t('Transfer selected agents')}</Button>}
      <Dialog.Close asChild><Button type="button" variant="outline" disabled={transfer.isPending}>{transfer.isSuccess ? t('Close') : t('Cancel')}</Button></Dialog.Close>
    </div>
  </div>
}

export function TransferAgents({ source }: { readonly source: AdminUser }) {
  const { t } = useTranslation()
  const { user, mustChangePassword } = useAuth()
  const [open, setOpen] = useState(false)
  if (!canTransferMemberAgents(user, source) || mustChangePassword || !source.tenant_id) return null
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><Button type="button" variant="outline" className="relative z-20 self-start" aria-label={t('Transfer agents from {name}', { name: source.display_name ?? source.email ?? source.id })}>{t('Transfer agents')}</Button></Dialog.Trigger>
    <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm" />
      <Dialog.Content className="fixed start-1/2 top-1/2 z-50 flex max-h-[90svh] w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 -translate-y-1/2 flex-col gap-5 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-card [overflow-wrap:anywhere]">
        <Dialog.Title className="font-display text-lg font-semibold">{t('Transfer agents')}</Dialog.Title>
        <TransferForm scope={{ tenantId: source.tenant_id, sourceUserId: source.id }} sourceName={source.display_name ?? source.email ?? source.id} />
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
