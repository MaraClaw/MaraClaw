import { Button } from '@/components/ui/button'
import type { useAgentSetup } from '@/hooks/use-agent-setup'
import type { AgentSetup } from '@/lib/agent-setup-api'

function setupMessage(setup: AgentSetup | undefined) {
  if (!setup) return { title: 'Checking setup status', detail: 'Start stays unavailable until setup status is confirmed.' }
  switch (setup.state) {
    case 'pending': return { title: 'Setup pending', detail: 'Setup is queued. Start stays unavailable until setup finishes.' }
    case 'running': return { title: 'Setup in progress', detail: 'Setup is running. Start stays unavailable until setup finishes.' }
    case 'failed': return { title: 'Setup failed', detail: setup.retryable ? 'Setup could not finish. Retry setup to try again.' : 'Setup could not finish. Contact your company admin for help.' }
    case 'succeeded': return { title: 'Setup complete', detail: 'This agent is ready to start.' }
    case null: return setup.setup_pending ? { title: 'Setup pending', detail: 'Start stays unavailable until setup finishes.' } : null
    default: return setup.state satisfies never
  }
}

export function AgentSetupStatus({ query }: { readonly query: ReturnType<typeof useAgentSetup> }) {
  const message = setupMessage(query.data)
  if (!message && !query.isError) return null
  return (
    <section aria-label="Agent setup" className="space-y-3 rounded-xl border border-border p-4">
      {query.isError ? <>
        <div role="alert" className="space-y-1 text-sm">
          <h2 className="font-medium text-destructive">Unable to check setup status</h2>
          <p className="text-muted-foreground">Start stays unavailable until status is confirmed. Try checking again.</p>
        </div>
        <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry status check</Button>
      </> : message ? <div role="status" aria-live="polite" className="space-y-1 text-sm">
        <h2 className="font-medium">{message.title}</h2>
        <p className="text-muted-foreground">{message.detail}</p>
      </div> : null}
      {query.retry.isError ? <p role="alert" className="text-sm text-destructive">Unable to retry setup. Check status and try again, or contact your company admin.</p> : null}
      {query.data?.state === 'failed' && query.data.retryable && !query.isError ? <Button
        variant="outline" className="h-auto min-h-11 whitespace-normal"
        disabled={query.retry.isPending || query.isFetching}
        onClick={() => query.retry.mutate()}
      >{query.retry.isPending ? 'Retrying setup…' : 'Retry setup'}</Button> : null}
    </section>
  )
}
