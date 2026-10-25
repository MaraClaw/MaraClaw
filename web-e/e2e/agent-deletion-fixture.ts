import { expect, type Page } from '@playwright/test'

import { originalAgent, originalToken, originalUser, prepareWorkspace } from './session-fencing-fixture'

export const settingsPath = `/app/agents/${originalAgent.id}/settings`
export const deletionRequest = {
  id: 'cfbc941b-097d-46d8-adbd-22ba6942a11c', agent_id: originalAgent.id, agent_name: originalAgent.name,
  status: 'pending', cancel_reason: null, requester: { id: originalUser.id, display_name: originalUser.display_name },
  created_at: '2026-10-25T00:00:00Z', resolved_at: null,
  progress: { counted_approvals: 1, required_approvals: 2, genesis_approved: false },
  reason: 'No longer needed', agent: null,
  viewer: { can_approve: false, has_viewed: false, has_approved: false }, approvals: [],
} as const

export async function prepareDeletion(page: Page, pending = false) {
  await prepareWorkspace(page)
  const posts: unknown[] = []
  const detailReads: string[] = []
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => {
    expect(route.request().method()).toBe('GET')
    return route.fulfill({ json: originalAgent })
  })
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route('**/api/agent-deletion-requests**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    if (url.pathname === `/api/agent-deletion-requests/${deletionRequest.id}`) {
      expect(request.method()).toBe('GET')
      detailReads.push(url.pathname)
      return route.fulfill({ json: deletionRequest })
    }
    expect(url.pathname).toBe('/api/agent-deletion-requests')
    switch (request.method()) {
      case 'GET':
        expect(Object.fromEntries(url.searchParams)).toEqual({ agent_id: originalAgent.id, status: 'pending' })
        return route.fulfill({ json: { items: pending ? [deletionRequest] : [], next_cursor: null } })
      case 'POST':
        posts.push(request.postDataJSON())
        return route.fulfill({ status: 201, json: deletionRequest })
      default:
        throw new Error(`Unexpected deletion request method: ${request.method()}`)
    }
  })
  return { posts, detailReads }
}

export async function deferDeletion(page: Page) {
  const requested = Promise.withResolvers<void>()
  const response = Promise.withResolvers<number>()
  await page.route('**/api/agent-deletion-requests', async (route) => {
    expect(route.request().method()).toBe('POST')
    requested.resolve()
    const status = await response.promise
    await route.fulfill({ status, json: status === 201 ? deletionRequest : { detail: 'Deletion request refused' } })
  })
  return { requested: requested.promise, release: (status = 201) => response.resolve(status) }
}
