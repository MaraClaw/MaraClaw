import * as Dialog from '@radix-ui/react-dialog'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { agentTransferOptions, transferSourceOptions, transferTargetsOptions } from '@/hooks/agent-transfer-options'
import { useAuth } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { agentLifecycleError } from '@/lib/agent-lifecycle-errors'
import { agentTransferSchema, canTransferMemberAgents, transferableAgents, type TransferScope } from '@/lib/agent-transfers-api'
import { type AdminUser } from '@/lib/users-api'

function TransferForm({ scope, sourceName }: { readonly scope: TransferScope; readonly sourceName: string }) {
  const recipientId = useId()
  const submitting = useRef(false)
  const [recipient, setRecipient] = useState('')
  const [selected, setSelected] = useState<readonly string[]>([])
  const [validation, setValidation] = useState('')
  const source = useQuery(transferSourceOptions(scope.sourceUserId, true))
  const targets = useQuery(transferTargetsOptions(scope, true))
  const transfer = useSessionMutation(agentTransferOptions(useQueryClient()))
  const { user } = useAuth()
  const sourceEligible = source.data && source.data.id === scope.sourceUserId && canTransferMemberAgents(user, source.data)
  const agents = sourceEligible ? transferableAgents(source.data) : []
  const ready = source.isSuccess && targets.isSuccess && !source.isFetching && !targets.isFetching && sourceEligible && !transfer.isPending && !transfer.isSuccess

  function submit() {
    if (!ready || submitting.current) return
    const target = targets.data?.find((member) => member.id === recipient)
    const parsed = agentTransferSchema.safeParse({ from_user_id: scope.sourceUserId, to_user_id: recipient, agent_ids: selected })
    if (!parsed.success || !target || selected.some((id) => !agents.some((agent) => agent.id === id))) {
      setValidation('Choose an active recipient in this organization and between 1 and 100 available agents.')
      return
    }
    setValidation('')
    submitting.current = true
    transfer.mutate(parsed.data, { onSettled: () => { submitting.current = false } })
  }

  return <div className="flex flex-col gap-5">
    <Dialog.Description className="text-sm leading-relaxed text-muted-foreground">Transfer agents owned by {sourceName} to an active member in this organization. This does not activate or deactivate either member. Pending deletion requests for transferred agents will be cancelled.</Dialog.Description>
    {source.isPending || targets.isPending ? <p role="status" className="text-sm">Loading agents and all eligible recipients...</p> : null}
    {source.isError || targets.isError ? <div className="flex flex-col items-start gap-3">
      <p role="alert" className="text-sm">Unable to load transfer options. Check your connection and try again.</p>
      <Button variant="outline" disabled={source.isFetching || targets.isFetching} onClick={() => { setSelected([]); void source.refetch(); void targets.refetch() }}>Reload transfer options</Button>
    </div> : null}
    {source.isSuccess && !sourceEligible ? <p role="alert" className="text-sm">This member is no longer in your organization or is not eligible. Close this dialog and refresh Users.</p> : null}
    {sourceEligible ? <>
      <fieldset disabled={!ready} className="flex min-w-0 flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">Owned non-system agents ({selected.length} / 100 selected)</legend>
        {agents.length === 0 ? <p className="text-sm text-muted-foreground">No transferable agents. Close this dialog to choose another member.</p> : <div className="max-h-64 overflow-y-auto rounded-xl border border-border p-3">
          {agents.map((agent) => <label key={agent.id} className="flex min-h-11 items-start gap-3 py-2 text-sm">
            <input type="checkbox" name="agent_ids" value={agent.id} className="mt-0.5 size-5 shrink-0 accent-primary" checked={selected.includes(agent.id)}
              disabled={!selected.includes(agent.id) && selected.length >= 100}
              onChange={(event) => { setSelected((values) => event.target.checked ? [...values, agent.id] : values.filter((id) => id !== agent.id)); setValidation('') }} />
            <span className="min-w-0"><span className="font-medium">{agent.name}</span><span className="mt-1 block text-xs text-muted-foreground">{agent.is_expired ? 'Expired' : agent.status}</span></span>
          </label>)}
        </div>}
      </fieldset>
      <div className="flex flex-col gap-2"><label htmlFor={recipientId} className="text-sm font-medium">Transfer to</label>
        <Select id={recipientId} name="to_user_id" value={recipient} disabled={!ready} onChange={(event) => { setRecipient(event.target.value); setValidation('') }} aria-describedby={`${recipientId}-hint`}>
          <option value="">Choose a member</option>
          {(targets.data ?? []).map((member) => <option key={member.id} value={member.id}>{member.display_name ?? member.email ?? member.id}{member.email && member.display_name ? ` (${member.email})` : ''}</option>)}
        </Select>
        <p id={`${recipientId}-hint`} className="text-xs text-muted-foreground">Active members and agent admins only. Organization admins and members of other organizations are excluded.</p>
        {targets.isSuccess && targets.data.length === 0 ? <p className="text-sm text-muted-foreground">No eligible recipients. Activate or add another member before transferring agents.</p> : null}
      </div>
    </> : null}
    <p role="alert" className="text-sm text-destructive">{validation || (transfer.isError ? agentLifecycleError(transfer.error) : '')}</p>
    <p role="status" className="text-sm">{transfer.isSuccess ? `${transfer.data.transferred.length} agents transferred. ${transfer.data.cancelled_deletion_request_ids.length} pending deletion requests cancelled.` : ''}</p>
    <div className="flex flex-wrap gap-3">
      {transfer.isSuccess ? null : <Button type="button" disabled={!ready || agents.length === 0 || targets.data?.length === 0} onClick={submit}>{transfer.isPending ? 'Transferring...' : 'Transfer selected agents'}</Button>}
      <Dialog.Close asChild><Button type="button" variant="outline" disabled={transfer.isPending}>{transfer.isSuccess ? 'Close' : 'Cancel'}</Button></Dialog.Close>
    </div>
  </div>
}

export function TransferAgents({ source }: { readonly source: AdminUser }) {
  const { user, mustChangePassword } = useAuth()
  const [open, setOpen] = useState(false)
  if (!canTransferMemberAgents(user, source) || mustChangePassword || !source.tenant_id) return null
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><Button type="button" variant="outline" className="relative z-20 self-start" aria-label={`Transfer agents from ${source.display_name ?? source.email ?? source.id}`}>Transfer agents</Button></Dialog.Trigger>
    <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm" />
      <Dialog.Content className="fixed start-1/2 top-1/2 z-50 flex max-h-[90svh] w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 -translate-y-1/2 flex-col gap-5 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-card [overflow-wrap:anywhere]">
        <Dialog.Title className="font-display text-lg font-semibold">Transfer agents</Dialog.Title>
        <TransferForm scope={{ tenantId: source.tenant_id, sourceUserId: source.id }} sourceName={source.display_name ?? source.email ?? source.id} />
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
