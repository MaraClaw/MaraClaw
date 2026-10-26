import { useEffect, useReducer } from 'react'

import { isAgentExpired } from '../lib/agent-lifecycle-policy.ts'
import { agentLifecycleKey } from './agent-lifecycle-options.ts'
import { useWorkspaceSession } from './session-workspace.ts'

export function useAgentExpiry(agent: {
  readonly id: string; readonly is_expired?: boolean; readonly expires_at?: string | null
}) {
  const scope = useWorkspaceSession()
  const [, render] = useReducer((value: number) => value + 1, 0)
  const { id, is_expired, expires_at } = agent
  useEffect(() => {
    if (is_expired || !expires_at || Date.parse(expires_at) <= Date.now()) return
    let timer: ReturnType<typeof setTimeout>
    const schedule = scope.effect(() => {
      const remaining = Date.parse(expires_at) - Date.now()
      if (remaining > 0) {
        timer = setTimeout(schedule, Math.min(remaining, 2_147_483_647))
        return
      }
      render()
      void scope.client.invalidateQueries({ queryKey: ['agent', id], exact: true })
      void scope.client.invalidateQueries({ queryKey: agentLifecycleKey(id), exact: true })
    })
    schedule()
    return () => clearTimeout(timer)
  }, [scope, id, is_expired, expires_at])
  return isAgentExpired(agent)
}
