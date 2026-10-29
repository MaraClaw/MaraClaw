import { expect, type Page } from '@playwright/test'
import { z } from 'zod'

import type { ScheduleOut } from '../src/lib/schedules-api'
import { originalAgent, originalToken, prepareWorkspace, replacementToken, replacementUser } from './session-fencing-fixture'

export const savedSchedule: ScheduleOut = {
  id: 'timezone-schedule', name: 'Morning report', instruction: 'Summarize updates',
  cron_expr: '0 9 * * *', timezone: 'Asia/Shanghai', is_enabled: true, notify_on_failure: false,
  next_run_at: '2026-01-15T01:00:00Z', last_run_at: null,
}
export const schedulesPath = `/app/agents/${originalAgent.id}/schedules`
export const schedulesApiPath = `/api/agents/${originalAgent.id}/schedules/`

const createSchema = z.object({
  name: z.string(), instruction: z.string(), cron_expr: z.string(),
  timezone: z.string(), is_enabled: z.boolean(), notify_on_failure: z.boolean(),
  ends_at: z.iso.datetime({ offset: true }).nullable().optional(),
}).strict()
const patchSchema = createSchema.partial()
const scheduleSchema = createSchema.extend({
  id: z.string(), next_run_at: z.string().nullable().optional(), last_run_at: z.string().nullable().optional(),
})
const nextRuns = new Map([
  ['UTC', '2026-01-15T09:00:00Z'], ['Asia/Shanghai', savedSchedule.next_run_at],
  ['America/New_York', '2026-01-15T14:00:00Z'],
  ['Etc/GMT-8', '2026-01-15T01:00:00Z'], ['Etc/GMT-7', '2026-01-15T02:00:00Z'],
  ['Etc/GMT+5', '2026-01-15T14:00:00Z'],
])

export async function prepareSchedules(page: Page, initial: readonly ScheduleOut[] = [], creator = true) {
  await prepareWorkspace(page)
  const agent = { ...originalAgent, creator_id: creator ? originalAgent.creator_id : replacementUser.id }
  await page.route(`**/api/agents/${agent.id}`, (route) => route.fulfill({ json: agent }))
  await page.route(`**/api/agents/${agent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  let schedules = [...initial]
  const creations: z.infer<typeof createSchema>[] = []
  const patches: z.infer<typeof patchSchema>[] = []
  await page.route(`**${schedulesApiPath}**`, async (route) => {
    const request = route.request()
    const authorization = request.headers().authorization
    if (request.method() !== 'GET') expect(authorization).toBe(`Bearer ${originalToken}`)
    switch (request.method()) {
      case 'GET':
        expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(authorization)
        await route.fulfill({ json: authorization === `Bearer ${originalToken}` && !request.url().endsWith('/history') ? schedules : [] })
        return
      case 'POST': {
        if (request.url().endsWith('/run')) return route.fulfill({ json: {
          status: 'triggered', schedule_id: new URL(request.url()).pathname.split('/').at(-2), run_id: 'fixture-manual-run',
        } })
        const body = createSchema.parse(request.postDataJSON())
        creations.push(body)
        const created = { ...savedSchedule, ...body, next_run_at: z.string().parse(nextRuns.get(body.timezone)) }
        schedules = [...schedules, created]
        await route.fulfill({ json: created })
        return
      }
      case 'PATCH': {
        const body = patchSchema.parse(request.postDataJSON())
        patches.push(body)
        const current = z.object({ id: z.string() }).passthrough().parse(schedules.find((item) => request.url().endsWith(`/${item.id}`)))
        schedules = schedules.map((item) => item.id === current.id ? scheduleSchema.parse({
          ...item, ...body, next_run_at: body.timezone ? z.string().parse(nextRuns.get(body.timezone)) : item.next_run_at,
        }) : item)
        await route.fulfill({ json: schedules.find((item) => item.id === current.id) })
        return
      }
      case 'DELETE':
        schedules = schedules.filter((item) => !request.url().endsWith(`/${item.id}`))
        await route.fulfill({ status: 204 })
        return
      default:
        throw new Error(`Unsupported schedule fixture method: ${request.method()}`)
    }
  })
  return { creations, patches }
}

export async function fillSchedule(page: Page) {
  await page.getByLabel('Name', { exact: true }).fill(savedSchedule.name)
  await page.getByLabel('Instruction', { exact: true }).fill(savedSchedule.instruction)
  await page.getByLabel('Repeat', { exact: true }).selectOption('daily')
  await page.getByLabel('Time', { exact: true }).fill('09:00')
  await page.getByLabel('Timezone', { exact: true }).selectOption('Etc/GMT-8')
}
