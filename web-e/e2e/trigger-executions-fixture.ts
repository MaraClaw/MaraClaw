import { expect, type Page } from '@playwright/test'

import type { TriggerResponse } from '../src/lib/engine-contracts.generated'
import type { TriggerExecution } from '../src/lib/trigger-executions-api'
import type { AgentOut } from '../src/lib/workspace-api'
import { originalAgent, originalToken, originalUser, prepareWorkspace } from './session-fencing-fixture'

export const tasksPath = `/app/agents/${originalAgent.id}/tasks`
export const primaryTrigger = {
  id: 'a7f14bc7-f566-4970-8f09-287886138248', name: 'Morning report', type: 'cron', is_enabled: true,
  config: { cron_expr: '0 9 * * *' }, cooldown_seconds: 0, fire_count: 0, reason: 'Prepare the morning report',
} satisfies TriggerResponse
export const secondaryTrigger = { ...primaryTrigger, id: '6e38f1b9-f97b-419c-9aee-f070b3e18444', name: 'Inbox monitor' }
export const historyPath = `/api/agents/${originalAgent.id}/triggers/${primaryTrigger.id}/executions`
export const secondaryHistoryPath = `/api/agents/${originalAgent.id}/triggers/${secondaryTrigger.id}/executions`
export const latestId = '851bc819-69cd-4f7c-9954-1cb73eddb841'
export const olderId = '54903fdd-0609-472d-a73c-6af08846e2b7'

export function historyExecution(id = latestId, status: TriggerExecution['status'] = 'pending'): TriggerExecution {
  return {
    id, trigger_id: primaryTrigger.id, agent_id: originalAgent.id, source: 'cron', status,
    scheduled_at: '2026-10-23T08:00:00+08:00', created_at: '2026-10-23T00:00:00Z',
    started_at: null, finished_at: null,
  }
}

export async function prepareTriggerHistory(page: Page, access: AgentOut['access_level'] = 'manage', role = 'member') {
  await prepareWorkspace(page)
  await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: { ...originalUser, role } }))
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: { ...originalAgent, access_level: access } }))
  await page.route(`**/api/agents/${originalAgent.id}/openclaw-update`, (route) => route.fulfill({
    json: { notice: null, run: null, required: false, can_upgrade: false },
  }))
  await prepareTriggerTasks(page, originalAgent.id)
  await page.route(`**/api/agents/${originalAgent.id}/focus/`, (route) => route.fulfill({ json: [] }))
  await page.route(`**/api/agents/${originalAgent.id}/triggers`, (route) => route.fulfill({ json: [primaryTrigger, secondaryTrigger] }))
  await page.route(`**${secondaryHistoryPath}*`, (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
}

export async function prepareTriggerTasks(page: Page, agentId: string) {
  await page.route(`**/api/agents/${agentId}/tasks/page?*`, (route) => {
    const request = route.request()
    expect(request.method()).toBe('GET')
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    const params = Object.fromEntries(new URL(request.url()).searchParams)
    expect(params).toEqual(params.status_filter === 'doing' ? { limit: '100', status_filter: 'doing' } : { limit: '50' })
    return route.fulfill({ json: { items: [], next_cursor: null } })
  })
  await page.route(`**/api/agents/${agentId}/files/?path=skills`, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    return route.fulfill({ json: [] })
  })
}

export function assertHistoryRequest(url: string, method: string, authorization: string | undefined) {
  expect(method).toBe('GET')
  expect(authorization).toBe(`Bearer ${originalToken}`)
  expect(new URL(url).searchParams.get('limit')).toBe('20')
}
