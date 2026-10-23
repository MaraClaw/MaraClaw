import { QueryClient } from '@tanstack/react-query'

import { createSessionQueryScope } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import type { AgentUpdate, OpenClawNotice, OpenClawRun } from '../src/lib/openclaw-updates-api.ts'

export const agentId = '22222222-2222-4222-8222-222222222222'
export const notice: OpenClawNotice = {
  id: '11111111-1111-4111-8111-111111111111', title: 'Runtime update',
  body: 'Full release details.\nSecond paragraph.', mandatory: true, created_at: '2026-10-22T09:00:00Z',
}
export const run: OpenClawRun = { id: '33333333-3333-4333-8333-333333333333', state: 'pending', failure_code: null }
export const update: AgentUpdate = { notice, run: null, required: true, can_upgrade: true }

export function fixture() {
  const store = authSession
  store.adoptStoredSession(store.capture())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false, gcTime: Infinity } } })
  const scope = createSessionQueryScope(store, store.capture(), client)
  return { store, client, scope, replace: () => store.adoptStoredSession(store.capture()) }
}
