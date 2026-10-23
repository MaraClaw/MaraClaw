import type { TriggerExecution } from '../src/lib/trigger-executions-api.ts'

export const execution: TriggerExecution = {
  id: '851bc819-69cd-4f7c-9954-1cb73eddb841',
  trigger_id: 'a7f14bc7-f566-4970-8f09-287886138248',
  agent_id: '5458ec51-a954-4306-8626-68e16921d95f',
  source: 'cron', status: 'pending', scheduled_at: '2026-10-23T08:00:00+08:00',
  started_at: null, finished_at: null, created_at: '2026-10-23T00:00:00Z',
}
export const target = { agentId: execution.agent_id, triggerId: execution.trigger_id }
export const firstPage = { items: [execution], next_cursor: 'opaque+/=? page' }
