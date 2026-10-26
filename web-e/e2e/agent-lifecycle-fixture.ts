import { expect, type Page } from '@playwright/test'

import type { AgentLifecycle } from '../src/lib/agent-lifecycle-api'
import type { AgentOut } from '../src/lib/workspace-api'
import { lifecycleReady, warningLifecycle } from './agent-lifecycle-data'
import { originalAgent, originalToken, prepareWorkspace, replacementToken } from './session-fencing-fixture'

export const settingsPath = `/app/agents/${originalAgent.id}/settings`
export const lifecyclePath = `**/api/agents/${originalAgent.id}/lifecycle`

export async function prepareLifecycle(page: Page, initial = warningLifecycle, agent: AgentOut = originalAgent) {
  await prepareWorkspace(page)
  let lifecycle = initial
  let status = 200
  let reads = 0
  let replacementReads = 0
  await page.route(`**/api/agents/${agent.id}`, (route) => route.fulfill({ json: agent }))
  await page.route(`**/api/agents/${agent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route('**/api/agent-deletion-requests?*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.route(lifecyclePath, (route) => {
    expect(route.request().method()).toBe('GET')
    if (route.request().headers().authorization === `Bearer ${replacementToken}`) {
      replacementReads++
      return route.fulfill({ json: { ...lifecycleReady, optional_mcp: [] } })
    }
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    reads++
    return route.fulfill({ status, json: status === 200 ? lifecycle : { detail: 'private-secret-status-trace' } })
  })
  return {
    reads: () => reads, replacementReads: () => replacementReads,
    set: (next: AgentLifecycle) => { lifecycle = next }, status: (next: number) => { status = next },
  }
}

export async function deferMCPRetry(page: Page) {
  const requested = Promise.withResolvers<void>()
  const response = Promise.withResolvers<number>()
  const bodies: unknown[] = []
  await page.route(`**/api/agents/${originalAgent.id}/setup/mcp-retry`, async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    bodies.push(route.request().postDataJSON())
    requested.resolve()
    const status = await response.promise
    await route.fulfill({ status, json: status === 202 ? {
      server_id: 'company/research', state: 'pending', warning_code: null, attempts: 2, retryable: false,
    } : { detail: 'private-secret-retry-trace' } })
  })
  return { requested: requested.promise, release: (status = 202) => response.resolve(status), bodies }
}
