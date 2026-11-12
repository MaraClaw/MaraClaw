import { useTranslation } from '@/hooks/use-translation'
import * as Dialog from '@radix-ui/react-dialog'
import { useRef, useState } from 'react'

import { McpServerForm } from '@/components/mcp-server-form'
import { Button } from '@/components/ui/button'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { createAgentMcpServer, patchAgentMcpServer, type McpServerList, type McpServerSummary } from '@/lib/agent-mcp-api'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { createMcpServerDraft, mcpServerChanges, mcpServerConfiguration } from '@/lib/mcp-server-draft'
import type { AgentOut } from '@/lib/workspace-api'
import { showRequestError } from '@/lib/request-error-toast'

export function McpServerEditor({ agentId, server, onClose, returnFocus, onAccessDenied, onSavedSyncPending }: {
  readonly agentId: string
  readonly server: McpServerSummary | null
  readonly onClose: () => void
  readonly returnFocus: HTMLElement
  readonly onAccessDenied: () => void
  readonly onSavedSyncPending: () => void
}) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const [initial] = useState(() => createMcpServerDraft(server ?? undefined))
  const [draft, setDraft] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const submitting = useRef(false)
  const save = useMutation({
    gcTime: 0,
    mutationFn: () => server
      ? patchAgentMcpServer({ agentId, name: server.name }, mcpServerChanges(draft, server), { owner: scope.owner })
      : createAgentMcpServer(agentId, mcpServerConfiguration(draft), { owner: scope.owner }),
    async onSuccess(result) {
      await scope.client.cancelQueries({ queryKey: ['agent-mcp-servers', agentId] })
      if (!scope.isCurrent() || scope.client.getQueryData<AgentOut>(['agent', agentId])?.access_level !== 'manage') return
      switch (result.sync) {
        case 'applied':
          scope.client.setQueryData<McpServerList>(['agent-mcp-servers', agentId], (previous) => ({
            servers: [...(previous?.servers ?? []).filter((item) => item.name !== result.value.name), result.value],
          }))
          break
        case 'pending':
          onSavedSyncPending()
          void scope.client.invalidateQueries({ queryKey: ['agent-mcp-servers', agentId] })
          break
        default: result satisfies never
      }
      onClose()
    },
    onError(failure) {
      showRequestError(userFacingRequestError(failure, translate("Unable to save this server. Check your connection and try again.")), failure)
      if (failure instanceof ApiError && failure.status === 403) onAccessDenied()
    },
    onSettled() { submitting.current = false },
  })
  function dismiss() {
    if (!scope.isCurrent() || submitting.current) return
    if (draft !== initial && !window.confirm(translate("Discard unsaved server changes?"))) return
    onClose()
  }
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) dismiss() }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
        <Dialog.Content onCloseAutoFocus={(event) => { event.preventDefault(); returnFocus.focus() }}
          className="fixed top-1/2 left-1/2 z-50 max-h-[90svh] w-[calc(100%_-_2rem)] max-w-2xl -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-elevated">
          <Dialog.Title className="font-display text-xl font-semibold">{server ? translate("Configure server") : translate("Add server")}</Dialog.Title>
          <Dialog.Description className="text-base text-muted-foreground">{translate("Stored configurations are synced to OpenClaw. Saved connection details and credential values are never displayed.")}</Dialog.Description>
          <form className="space-y-6" onSubmit={(event) => {
            event.preventDefault()
            if (!scope.isCurrent() || submitting.current) return
            const fields = draft.transport === 'stdio' ? draft.env : draft.headers
            if (new Set(fields.map((field) => draft.transport === 'stdio' ? field.key : field.key.toLowerCase())).size !== fields.length) {
              setError(translate("Each header or environment variable must have a different name."))
              return
            }
            setError(null)
            submitting.current = true
            save.mutate()
          }}>
            <fieldset disabled={save.isPending} className="min-w-0">
              <McpServerForm draft={draft} server={server} onChange={scope.effect(setDraft)} />
            </fieldset>
            {error ? <p role="status" className="text-sm text-destructive">{error}</p> : null}
            {save.isPaused ? <p role="status" className="text-base text-muted-foreground">{translate("You’re offline. Saving will resume when the connection returns.")}</p> : null}
            <div className="flex flex-wrap justify-end gap-3">
              <Button type="button" variant="outline" disabled={save.isPending} onClick={dismiss}>{translate("Cancel")}</Button>
              <Button type="submit" disabled={save.isPending} className="min-w-32">{save.isPending ? 'Saving...' : translate("Save server")}</Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
