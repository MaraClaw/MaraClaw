import { queryOptions } from '@tanstack/react-query'

import { fetchBroadcastEmailStatus, type BroadcastId } from '../lib/broadcast-api.ts'

export function broadcastEmailStatusOptions(id: BroadcastId) {
  return queryOptions({
    queryKey: ['admin-broadcast-email', id] as const,
    queryFn: ({ signal }) => fetchBroadcastEmailStatus(id, signal),
    retry: false,
    staleTime: 0,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const status = query.state.data
      return query.state.status !== 'error' && status?.available && status.queued + status.claimed > 0 ? 5_000 : false
    },
  })
}
