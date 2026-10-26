import { onlineManager } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { agentLifecycleOptions, retryMCPOptions } from './agent-lifecycle-options.ts'
import { useMutation, useQuery, useWorkspaceSession } from './session-workspace.ts'

export function useAgentLifecycle(agentId: string, canManage: boolean) {
  const scope = useWorkspaceSession()
  const [connection, setConnection] = useState(() => ({ online: onlineManager.isOnline(), disconnectedAt: 0 }))
  useEffect(() => onlineManager.subscribe((online) => setConnection((current) => ({
    online, disconnectedAt: online ? current.disconnectedAt : Date.now(),
  }))), [])
  const query = useQuery(agentLifecycleOptions(scope, agentId, canManage))
  const options = useMemo(() => retryMCPOptions(scope, agentId), [scope, agentId])
  const retry = useMutation(options)
  const confirmed = connection.online && !query.isPaused && query.dataUpdatedAt > connection.disconnectedAt && query.isFetchedAfterMount && !query.isError && Boolean(query.data)
  return { ...query, online: connection.online, confirmed, retry, refetch: scope.effect(query.refetch) }
}
