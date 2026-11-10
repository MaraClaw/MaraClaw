import { Button } from '@/components/ui/button'
import type { useAgentSetup } from '@/hooks/use-agent-setup'
import type { AgentSetup } from '@/lib/agent-setup-api'

import { RequestErrorToast } from '@/components/request-error-toast'
function setupMessage(setup: AgentSetup | undefined) {
  if (!setup) return { title: 'Checking setup status', detail: 'Start stays unavailable until setup status is confirmed.' }
  switch (setup.state) {
    case 'pending': return { title: 'Setup pending', detail: 'Setup is queued. Start stays unavailable until setup finishes.' }
    case 'running': return { title: 'Setup in progress', detail: 'Setup is running. Start stays unavailable until setup finishes.' }
    case 'failed': return { title: 'Setup failed', detail: setup.retryable ? 'Setup could not finish. Retry setup to try again.' : 'Setup could not finish. Contact your company admin for help.' }
    case 'succeeded': return { title: 'Setup complete', detail: 'Required setup has finished.' }
    case null: return setup.setup_pending ? { title: 'Setup pending', detail: 'Start stays unavailable until setup finishes.' } : null
    default: return setup.state satisfies never
  }
}

export function AgentSetupStatus({ query, retryAllowed = true }: { readonly query: ReturnType<typeof useAgentSetup>; readonly retryAllowed?: boolean }) {
  const message = setupMessage(query.data)
  if (!message && !query.isError) return null
  return (
    <section aria-label="Agent setup" className="space-y-3 rounded-xl border border-border p-4 text-[1.0625rem] leading-normal md:text-sm [overflow-wrap:anywhere] [&_.text-muted-foreground]:text-[0.9375rem] md:[&_.text-muted-foreground]:text-sm">
      {query.isError ? <>
        <div className="space-y-1">
          <RequestErrorToast error={query.error} message="Unable to check setup status. Try checking again." />
          <p className="text-muted-foreground">Start stays unavailable until status is confirmed. Try checking again.</p>
        </div>
        <Button variant="outline" className="h-auto min-h-11 whitespace-normal text-[1.0625rem] md:text-sm" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry status check</Button>
      </> : message ? <div role="status" aria-live="polite" className="space-y-1">
        <h2 className="font-medium">{message.title}</h2>
        <p className="text-muted-foreground">{message.detail}</p>
      </div> : null}
      {query.retry.isError ? <RequestErrorToast error={query.retry.error} message="Unable to retry setup. Check status and try again, or contact your company admin." /> : null}
      {query.data?.state === 'failed' && query.data.retryable && !query.isError ? <Button
        variant="outline" className="h-auto min-h-11 whitespace-normal text-[1.0625rem] md:text-sm"
        disabled={query.retry.isPending || query.isFetching || !retryAllowed}
        onClick={() => query.retry.mutate()}
      >{query.retry.isPending ? 'Retrying setup…' : 'Retry setup'}</Button> : null}
    </section>
  )
}
