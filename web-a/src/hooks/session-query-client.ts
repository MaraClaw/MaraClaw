import { QueryClient } from '@tanstack/react-query'

import type { SessionOwner, SessionPolicy } from '../lib/session-policy.ts'

export function createSessionQueryScope(owner: SessionOwner) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false },
    },
  })
  return { owner, client }
}

export function ownQueryClient(policy: SessionPolicy, owner: SessionOwner, client: QueryClient) {
  const retire = () => {
    void client.cancelQueries()
    client.clear()
    // Transport cancellation retires local work, not accepted server writes.
  }
  const unsubscribe = policy.subscribe(() => {
    if (!policy.isCurrent(owner)) retire()
  })
  if (!policy.isCurrent(owner)) retire()
  return () => {
    unsubscribe()
    // StrictMode reconnects the same client. Clear only on session retirement.
    void client.cancelQueries()
  }
}
