import type { TestContext } from 'node:test'

import { adminSession } from '../src/lib/admin-session.ts'
import { MemoryStorage } from './helpers.ts'

export const auditEvent = {
  id: 'b0e03217-e53b-4e50-b4a8-1488d7d92b31',
  actor_id: null, actor_role: 'platform_admin', actor_email: null,
  action: 'company.create', target_type: 'company', target_id: null, tenant_id: null,
  changes: { enabled: { before: false, after: true } }, details: { note: '<script>literal text</script>', counts: [1, null, true] },
  ip_address: null, created_at: null,
}
export const tenantId = 'a8122022-c233-4f10-91df-91e2d065d2cb'
export const actorId = '66dccce3-bcfb-40ef-b334-49c2b47d614d'

export function ownAuditSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const storage = new MemoryStorage()
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: storage } })
  adminSession.replace('synthetic-audit-token')
  context.after(() => {
    adminSession.logout()
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  return adminSession
}
