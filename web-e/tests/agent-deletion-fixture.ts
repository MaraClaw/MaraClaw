import type { TestContext } from 'node:test'

import { authSession } from '../src/lib/auth-session.ts'

export const agentId = '5458ec51-a954-4306-8626-68e16921d95f'
export const requestId = 'cfbc941b-097d-46d8-adbd-22ba6942a11c'
export const person = { id: '47f8d9a9-17eb-4b58-a0e3-fbc6a00d18db', display_name: 'Creator' }
export const deletionSummary = {
  id: requestId, agent_id: agentId, agent_name: 'Agent', status: 'pending', cancel_reason: null,
  requester: person, created_at: '2026-10-25T00:00:00Z', resolved_at: null,
  progress: { counted_approvals: 0, required_approvals: 2, genesis_approved: false },
} as const
export const deletionDetail = {
  ...deletionSummary, reason: 'No longer needed',
  agent: { id: agentId, name: 'Agent', role_description: 'Assistant', access_mode: 'user', status: 'stopped', last_active_at: null, creator: person, is_private: true },
  viewer: { can_approve: false, has_viewed: false, has_approved: false }, approvals: [],
} as const

export function deletionSession(context: TestContext) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'window')
  let token: string | null = null
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: {
    getItem: () => token, setItem: (_key: string, value: string) => { token = value }, removeItem: () => { token = null },
  } } })
  authSession.adoptStoredSession(authSession.capture())
  const user = { ...person, email: 'creator@example.test', role: 'member', tenant_id: 'tenant-one', email_verified: true } as const
  authSession.applySession(authSession.capture(), { access_token: 'deletion-original', user })
  context.after(() => {
    authSession.logout(authSession.capture())
    if (descriptor) Object.defineProperty(globalThis, 'window', descriptor)
    else Reflect.deleteProperty(globalThis, 'window')
    authSession.adoptStoredSession(authSession.capture())
  })
  return { owner: authSession.capture(), user }
}
