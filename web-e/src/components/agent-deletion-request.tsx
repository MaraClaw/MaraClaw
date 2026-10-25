import * as Dialog from '@radix-ui/react-dialog'
import { useId, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import {
  deletionReasonSchema, getAgentDeletionRequest, listPendingAgentDeletions,
  pendingDeletionRequestId, requestAgentDeletion,
} from '@/lib/agent-deletion-api'
import { userFacingRequestError } from '@/lib/http'

export function AgentDeletionRequest({ agentId, agentName, upgrading }: {
  readonly agentId: string
  readonly agentName: string
  readonly upgrading: boolean
}) {
  const scope = useWorkspaceSession()
  const reasonId = useId()
  const reasonInput = useRef<HTMLTextAreaElement>(null)
  const submitting = useRef(false)
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [reasonError, setReasonError] = useState<string | null>(null)
  const [requestId, setRequestId] = useState<string | null>(null)
  const queryKey = ['agent-deletion-requests', agentId, requestId ?? 'pending']
  const requests = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const context = { owner: scope.owner, signal }
      if (!requestId) return listPendingAgentDeletions(agentId, context)
      const detail = await getAgentDeletionRequest(requestId, context)
      return { items: detail.status === 'pending' ? [detail] : [], next_cursor: null }
    },
    staleTime: 0,
    retry: false,
    refetchInterval: 30_000,
  })
  const pending = requests.data?.items.find((item) => item.status === 'pending')
  const knownPending = Boolean(pending || (requestId && !requests.data))
  const submit = useMutation({
    mutationFn: (value: string) => requestAgentDeletion({ agent_id: agentId, reason: value }, { owner: scope.owner }),
    onSuccess(detail) {
      scope.client.setQueryData(['agent-deletion-requests', agentId, detail.id], {
        items: detail.status === 'pending' ? [detail] : [], next_cursor: null,
      })
      setRequestId(detail.id)
      setReason('')
      setOpen(false)
    },
    onError(failure) {
      const existing = pendingDeletionRequestId(failure)
      if (existing) {
        setRequestId(existing)
        setOpen(false)
        return
      }
      setError(userFacingRequestError(failure, 'Unable to request deletion. Check your connection and try again.'))
    },
    onSettled() { submitting.current = false },
  })
  const available = !upgrading && !submit.isPending && !knownPending && Boolean(requests.data) && !requests.isError

  return (
    <div className="space-y-2">
      <Dialog.Root open={open} onOpenChange={scope.effect((next) => {
        if (next && !available) return
        setOpen(next)
        if (next) { setError(null); setReasonError(null) }
      })}>
        <Dialog.Trigger asChild>
          <Button type="button" size="sm" variant="outline" className="min-h-11 aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
            aria-disabled={!available} onClick={(event) => { if (!available) event.preventDefault() }}>
            Request deletion
          </Button>
        </Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content
            className="fixed top-1/2 left-1/2 z-50 max-h-[90svh] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-elevated">
            <Dialog.Title className="font-display text-xl font-semibold">Request deletion</Dialog.Title>
            <Dialog.Description className="text-sm text-muted-foreground">
              Request deletion of {agentName}. Nothing is deleted until two distinct organization admins or the genesis admin approve.
            </Dialog.Description>
            <form className="space-y-4" noValidate onSubmit={(event) => {
              event.preventDefault()
              if (!scope.isCurrent() || !available || submitting.current) return
              const parsed = deletionReasonSchema.safeParse(reason)
              if (!parsed.success) {
                setReasonError(parsed.error.issues[0]?.message ?? 'Enter a reason.')
                reasonInput.current?.focus()
                return
              }
              setReasonError(null)
              setError(null)
              submitting.current = true
              submit.mutate(parsed.data)
            }}>
              <div className="space-y-2">
                <Label htmlFor={reasonId}>Reason</Label>
                <Textarea ref={reasonInput} id={reasonId} name="reason" value={reason} disabled={submit.isPending}
                  aria-invalid={Boolean(reasonError)} aria-describedby={`${reasonId}-help${reasonError ? ` ${reasonId}-error` : ''}`}
                  onChange={scope.effect((event) => { setReason(event.target.value); setReasonError(null); setError(null) })} />
                <p id={`${reasonId}-help`} className="text-sm text-muted-foreground">Required. Use 1–1,000 characters after trimming spaces.</p>
                {reasonError ? <p id={`${reasonId}-error`} role="alert" className="text-sm text-destructive">{reasonError}</p> : null}
              </div>
              {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
              <div className="flex flex-wrap justify-end gap-2">
                <Dialog.Close asChild><Button type="button" variant="outline" className="min-h-11">Cancel</Button></Dialog.Close>
                <Button type="submit" className="min-h-11" disabled={submit.isPending || !available}>
                  {submit.isPending ? 'Submitting…' : 'Request deletion'}
                </Button>
              </div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <p role="status" className="text-sm text-muted-foreground">
        {knownPending ? `Deletion request pending.${pending ? ` ${pending.progress.counted_approvals} of ${pending.progress.required_approvals} organization admin approvals.` : ''} Nothing is deleted until two distinct organization admins or the genesis admin approve.`
          : requests.isPending ? 'Checking deletion request status…' : ''}
      </p>
      {requests.isError ? <div className="space-y-2">
        <p role="alert" className="text-sm text-destructive">Unable to load deletion request status. Retry before submitting another request.</p>
        <Button type="button" size="sm" variant="outline" className="min-h-11" disabled={requests.isFetching}
          onClick={scope.effect(() => { void requests.refetch() })}>Retry status</Button>
      </div> : null}
    </div>
  )
}
