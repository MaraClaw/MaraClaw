import { expect, type Page } from '@playwright/test'

import type { ActivityItem } from '../src/lib/activity-api'
import { originalAgent, originalToken, originalUser, prepareWorkspace } from './session-fencing-fixture'

export const activityPath = `/app/agents/${originalAgent.id}/activity`
export const activityApiPath = `/api/agents/${originalAgent.id}/activity/page`
export const activity = {
  id: 'activity-latest', action_type: 'future.action', summary: 'Manager-only recorded action', created_at: null,
} satisfies ActivityItem

export async function prepareActivity(page: Page, access: 'use' | 'manage' = 'manage', role = 'member') {
  await prepareWorkspace(page)
  await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: { ...originalUser, role } }))
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: { ...originalAgent, access_level: access } }))
}

export function assertActivityRequest(request: { url(): string; method(): string; postData(): string | null; headers(): Record<string, string> }) {
  expect(request.method()).toBe('GET')
  expect(request.postData()).toBeNull()
  expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
  expect(new URL(request.url()).searchParams.get('limit')).toBe('20')
}
