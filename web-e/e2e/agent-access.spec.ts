import { expect, test } from '@playwright/test'

import { prepareChat } from './chat-identity-fixture'
import { originalAgent, originalToken, originalUser, prepareWorkspace, replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'

const reasons = [
  ['creator', 'You created this agent'],
  ['administrator', 'Available through administrator access'],
  ['company', 'Shared with your company'],
  ['direct', 'Shared directly with you'],
  ['department', 'Shared with your department'],
] as const

for (const [reason, label] of reasons) {
  test(`server-derived ${reason} explanation appears on cards, but not in agent sections`, async ({ page }) => {
    // Given
    await prepareChat(page)
    let reads = 0
    const agent = { ...originalAgent, creator_id: originalUser.id, access_level: 'manage', access_reason: reason }
    await page.route('**/api/agents/page?*', (route) => route.fulfill({ json: { items: [agent], next_cursor: null, has_more: false } }))
    await page.route(`**/api/agents/${agent.id}`, (route) => { reads++; return route.fulfill({ json: agent }) })
    await page.route(`**/api/agents/${agent.id}/metrics`, (route) => route.fulfill({ json: {} }))
    await page.route('**/api/agent-deletion-requests?*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
    await page.goto('/app/agents')
    await expect(page.getByText(label, { exact: true })).toBeVisible()
    // When
    await page.getByRole('link', { name: new RegExp(agent.name) }).click()
    // Then
    await expect(page).toHaveURL(`/app/agents/${agent.id}/chat`)
    await expect(page.getByText(label, { exact: true })).toHaveCount(0)
    await expect(page.getByRole('textbox', { name: `Message ${agent.name}` })).toBeVisible()
    await page.getByRole('navigation', { name: 'Agent sections' }).getByRole('link', { name: 'Settings', exact: true }).click()
    await expect(page.getByRole('heading', { name: agent.name, exact: true })).toBeVisible()
    await expect(page.getByText(label, { exact: true })).toHaveCount(0)
    expect(reads).toBe(1)
  })
}

test('session replacement discards the old agent while a shared detail read is pending', async ({ page }) => {
  // Given
  await prepareWorkspace(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/agents/${originalAgent.id}`, async (route) => {
    if (route.request().headers().authorization === `Bearer ${originalToken}`) {
      requested.resolve()
      await release.promise
      return route.fulfill({ json: { ...originalAgent, name: 'Original session agent', access_reason: 'creator' } })
    }
    expect(route.request().headers().authorization).toBe(`Bearer ${replacementToken}`)
    return route.fulfill({ json: { ...originalAgent, name: 'Replacement session agent', creator_id: replacementUser.id, access_reason: 'department' } })
  })
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route('**/api/agent-deletion-requests?*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  try {
    await page.goto(`/app/agents/${originalAgent.id}/settings`)
    await requested.promise
    // When
    await writeFromOtherTab(page, replacementToken)
    const replacement = page.getByRole('heading', { name: 'Replacement session agent', exact: true })
    await expect(replacement).toBeVisible()
    release.resolve()
    // Then
    await expect(page.getByRole('heading', { name: 'Original session agent', exact: true })).toHaveCount(0)
    await expect(replacement).toBeVisible()
  } finally { release.resolve() }
})
