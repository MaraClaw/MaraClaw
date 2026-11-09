import { expect, test } from '@playwright/test'
import { prepareAgentTools, tool, toolId, toolsPath } from './agent-tools-fixture'
import { originalAgent, writeFromOtherTab } from './session-fencing-fixture'

test('field save preserves secrets, probes saved plus draft, and removal restores inheritance', async ({ page }) => {
  const fixture = await prepareAgentTools(page)
  await page.goto(toolsPath)
  await page.getByRole('button', { name: tool.display_name }).click()
  await expect(page.getByLabel('Privileged proxy operation')).toHaveCount(0)
  await expect(page.locator('input[type=password]')).toHaveCount(0)
  await page.getByLabel('Region operation').selectOption('set')
  await page.getByLabel('New Region', { exact: true }).fill('ap-south-1')
  await page.getByRole('button', { name: 'Test saved + draft' }).click()
  await expect(page.getByText(/Connection test passed/)).toBeVisible()
  expect(fixture.probes).toEqual([{ saved: { agent_id: originalAgent.id, tool_id: toolId }, operations: { set: { region: 'ap-south-1' }, unset: [] } }])
  await page.getByRole('button', { name: 'Save config', exact: true }).click()
  await expect(page.getByText('Configuration saved. Unchanged values were preserved.')).toBeVisible()
  expect(fixture.writes).toEqual([{ set: { region: 'ap-south-1' }, unset: [] }])
  await page.getByLabel('Region operation').selectOption('unset')
  await page.getByRole('button', { name: 'Save config', exact: true }).click()
  await expect(page.getByText('Using an inherited value.', { exact: true })).toBeVisible()
  expect(fixture.writes.at(-1)).toEqual({ set: {}, unset: ['region'] })
  await page.getByLabel('API key operation').selectOption('set')
  await expect(page.getByLabel('New API key', { exact: true })).toHaveValue('')
})

test('failed writes retain a secret only in the current draft and discard clears it', async ({ page }) => {
  const fixture = await prepareAgentTools(page)
  fixture.status = 503
  await page.goto(toolsPath)
  await page.getByRole('button', { name: tool.display_name }).click()
  await page.getByLabel('API key operation').selectOption('set')
  await page.getByLabel('New API key', { exact: true }).fill('synthetic-new-key')
  await page.getByRole('button', { name: 'Save config', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Could not save')
  await expect(page.getByLabel('New API key', { exact: true })).toHaveValue('synthetic-new-key')
  await page.getByRole('button', { name: 'Discard draft' }).click()
  await expect(page.locator('input[type=password]')).toHaveCount(0)
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-new-key')
})

test('successful save refreshes configuration after same-session collapse and reopen', async ({ page }) => {
  const fixture = await prepareAgentTools(page)
  const entered = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/tools/agents/${originalAgent.id}/tool-config/${toolId}`, async (route) => {
    if (route.request().method() === 'GET') return route.fallback()
    entered.resolve(); await release.promise; await route.fallback()
  })
  try {
    await page.goto(toolsPath)
    const expand = page.getByRole('button', { name: tool.display_name })
    await expand.click()
    await page.getByLabel('Region operation').selectOption('set')
    await page.getByLabel('New Region', { exact: true }).fill('saved-after-collapse')
    await page.getByRole('button', { name: 'Save config', exact: true }).click()
    await entered.promise
    await expand.click()
    release.resolve()
    await expect.poll(() => fixture.writes.length).toBe(1)
    await expand.click()
    await expect(page.getByText('saved-after-collapse', { exact: true })).toBeVisible()
    await expect(page.getByText(/Configuration saved/)).toHaveCount(0)
  } finally { release.resolve() }
})

test('late saved probe has no feedback after a cross-tab logout', async ({ page }) => {
  await prepareAgentTools(page)
  const entered = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/tools/test-mcp', async (route) => { entered.resolve(); await release.promise; await route.fulfill({ json: { ok: true, tools: [] } }) })
  try {
    await page.goto(toolsPath)
    await page.getByRole('button', { name: tool.display_name }).click()
    await page.getByRole('button', { name: 'Test saved + draft' }).click()
    await entered.promise
    await writeFromOtherTab(page, null)
    await expect(page).toHaveURL(/\/login/)
    release.resolve()
    await expect(page.getByText(/Connection test passed/)).toHaveCount(0)
  } finally { release.resolve() }
})

for (const status of [200, 403]) test(`retired field save ${status} cannot restore a draft or feedback`, async ({ page }) => {
  await prepareAgentTools(page)
  const entered = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/tools/agents/${originalAgent.id}/tool-config/${toolId}`, async (route) => {
    if (route.request().method() === 'GET') return route.fallback()
    entered.resolve(); await release.promise
    await route.fulfill({ status, json: status === 200 ? { ok: true } : { detail: 'Retired write denied' } })
  })
  try {
    await page.goto(toolsPath)
    await page.getByRole('button', { name: tool.display_name }).click()
    await page.getByLabel('Region operation').selectOption('set')
    await page.getByLabel('New Region', { exact: true }).fill('retired-region')
    await page.getByRole('button', { name: 'Save config', exact: true }).click()
    await entered.promise
    await writeFromOtherTab(page, null)
    await expect(page).toHaveURL(/\/login/)
    release.resolve()
    await expect(page.getByText(/Configuration saved|Could not save/)).toHaveCount(0)
  } finally { release.resolve() }
})

for (const width of [390, 1440, 1920]) for (const theme of ['light', 'dark'] satisfies readonly ('light' | 'dark')[]) {
  test(`supported configuration fields reflow at ${width} in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    await prepareAgentTools(page)
    await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
    await page.goto(toolsPath)
    await page.getByRole('button', { name: tool.display_name }).click()
    await expect(page.getByLabel('Region operation')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: info.outputPath(`agent-tools-${width}-${theme}.png`), fullPage: true })
  })
}
