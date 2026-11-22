import { expect, type Page } from '@playwright/test'

import type { ScheduleHistoryItem } from '../src/lib/schedules-api'
import { savedSchedule, schedulesApiPath } from './schedules-fixture'
import { originalAgent, originalToken, originalUser } from './session-fencing-fixture'

export const historyPath = `${schedulesApiPath}${savedSchedule.id}/history/page`

export function historyRun(id: string, status: ScheduleHistoryItem['status'] = 'unknown'): ScheduleHistoryItem {
  return { id, created_at: '2026-10-20T00:00:00Z', summary: id, instruction: 'Report updates', reply: 'Partial output', status }
}

export async function prepareHistoryNavigation(page: Page) {
  const session = {
    id: '65dc0777-401c-4432-8e29-ecb4297f6f45', agent_id: originalAgent.id, user_id: originalUser.id,
    title: 'Scheduled agent conversation', source_channel: 'web', is_primary: true, writable: true,
    created_at: '2030-01-01T00:00:00Z', message_count: 0, unread_count: 0,
  }
  await page.route(`**/api/agents/${originalAgent.id}/sessions`, (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(route.request().postDataJSON()).toEqual({})
    return route.fulfill({ json: session })
  })
  await page.route('**/api/auth/chat-ticket', (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(route.request().postDataJSON()).toEqual({ agent_id: originalAgent.id, session_id: session.id })
    return route.fulfill({ json: { ticket: 'schedule-history-navigation-ticket', session_id: session.id } })
  })
  await page.route(`**/api/agents/${originalAgent.id}/sessions/page?*`, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(Object.fromEntries(new URL(route.request().url()).searchParams)).toEqual({ limit: '50', scope: 'mine' })
    return route.fulfill({ json: { items: [session], next_cursor: null, has_more: false } })
  })
  await page.route(`**/api/agents/${originalAgent.id}/sessions/${session.id}/messages/page?*`, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(Object.fromEntries(new URL(route.request().url()).searchParams)).toEqual({ limit: '50' })
    return route.fulfill({ json: { items: [], next_cursor: null, has_more: false, revision: null } })
  })
  await page.route(`**/api/agents/${originalAgent.id}/files/?path=skills`, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    return route.fulfill({ json: [] })
  })
}
