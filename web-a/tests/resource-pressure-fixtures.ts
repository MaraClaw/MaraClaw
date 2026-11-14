import type { TestContext } from 'node:test'

import { adminSession } from '../src/lib/admin-session.ts'
import { MemoryStorage } from './helpers.ts'

export const pressureToken = 'synthetic-pressure-token'
export const tenantId = 'a8122022-c233-4f10-91df-91e2d065d2cb'
export const otherTenantId = '5b9d6c1e-4a0f-4c55-8a4e-0d1c2f3a4b5c'
export const agentId = '66dccce3-bcfb-40ef-b334-49c2b47d614d'
export const episodeCursor = 'eyJ2ZXJzaW9uIjoxfQ'

export const summaryRow = {
  tenant_id: tenantId, tenant_name: 'Acme Corp', current_tier: 'pro',
  episode_counts: { memory: 4, cpu: 1, workspace_disk: 0, managed_storage: 2 },
  oom_count: 1, pressure_seconds: 7_500, max_peak_ratio: 0.9734, agents_affected: 3,
  last_seen_at: '2026-11-12T08:30:00Z', suggested_tier: 'max',
}
export const quietSummaryRow = {
  ...summaryRow, tenant_id: otherTenantId, tenant_name: 'Globex', current_tier: null, suggested_tier: null,
  episode_counts: { memory: 0, cpu: 0, workspace_disk: 1, managed_storage: 0 },
  oom_count: 0, pressure_seconds: 0, max_peak_ratio: 0.9, agents_affected: 1,
}

export const episodeRow = {
  id: 'b0e03217-e53b-4e50-b4a8-1488d7d92b31', tenant_id: tenantId, tenant_name: 'Acme Corp',
  agent_id: agentId, agent_name: 'Sales assistant', tier: 'pro', resource: 'memory', kind: 'threshold',
  limit_value: 2 * 1024 ** 3, peak_value: 1_990_000_000, peak_ratio: 0.9267, threshold_ratio: 0.9,
  started_at: '2026-11-12T08:00:00Z', last_seen_at: '2026-11-12T08:30:00Z', ended_at: null,
  sample_count: 7, detail: { container: 'guest' },
}
export const cpuEpisodeRow = {
  ...episodeRow, id: '0f9f5c1d-1f0e-4f1c-9a27-8d4d6c0b7a11', agent_id: null, agent_name: 'Removed agent',
  resource: 'cpu', kind: 'oom_kill', limit_value: 2000, peak_value: 1900, peak_ratio: 0.95,
  ended_at: '2026-11-12T09:00:00Z',
}

export const csvBody = 'id,tenant_id,tenant_name\r\nb0e03217-e53b-4e50-b4a8-1488d7d92b31,a8122022-c233-4f10-91df-91e2d065d2cb,Acme Corp\r\n'

export function ownPressureSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: new MemoryStorage() } })
  adminSession.replace(pressureToken)
  context.after(() => {
    adminSession.logout()
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  return adminSession
}
