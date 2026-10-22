import { expect, type Page } from '@playwright/test'
import { z } from 'zod'

import type { ScheduleOut, ScheduleRunReceipt } from '../src/lib/schedules-api'
import { prepareSchedules, savedSchedule, schedulesApiPath } from './schedules-fixture'
import { originalAgent, originalUser } from './session-fencing-fixture'

export const runPath = `${schedulesApiPath}${savedSchedule.id}/run`
export const manualAcceptance = { status: 'triggered', schedule_id: savedSchedule.id, run_id: 'durable-manual-run' } as const
export const receiptPath = `${schedulesApiPath}${savedSchedule.id}/runs/${manualAcceptance.run_id}`

export function runReceipt(status: ScheduleRunReceipt['status'] = 'pending', runId: string = manualAcceptance.run_id): ScheduleRunReceipt {
  return {
    run_id: runId, schedule_id: savedSchedule.id, agent_id: originalAgent.id, requester_id: originalUser.id,
    status, created_at: '2026-10-22T06:00:00Z', started_at: status === 'pending' ? null : '2026-10-22T06:01:00Z',
    finished_at: status === 'pending' || status === 'running' ? null : '2026-10-22T06:02:00Z', can_cancel: status === 'pending',
  }
}

export async function prepareRunSchedules(page: Page, initial: readonly ScheduleOut[] = [savedSchedule]) {
  const fixture = await prepareSchedules(page, initial)
  await page.route(`**${runPath}`, (route) => route.fulfill({ json: manualAcceptance }))
  await page.route(`**${schedulesApiPath}${savedSchedule.id}/runs/**`, (route) => {
    const cancelling = route.request().url().endsWith('/cancel')
    expect(route.request().method()).toBe(cancelling ? 'POST' : 'GET')
    expect(route.request().postData()).toBeNull()
    expect(route.request().headers()['idempotency-key']).toBeUndefined()
    const runId = z.string().parse(new URL(route.request().url()).pathname.split('/').at(cancelling ? -2 : -1))
    return route.fulfill({ json: runReceipt(cancelling ? 'cancelled' : 'pending', runId) })
  })
  return fixture
}
