import { test, expect } from '@playwright/test'
import { agentPath, discoveryKey, prepareDiscovery } from './workspace-discovery-fixture'
import { navigateChat } from './chat-identity-fixture'
import { replacementAgent, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

test('Pages directory changes cancel only the previous files query, and late content stays absent', async ({ page }, info) => {
  const discovery = await prepareDiscovery(page)
  const published = discovery.hold(discoveryKey('pages'))
  const directory = discovery.hold(discoveryKey('files', 'workspace/held'))
  try {
    await page.goto(agentPath('pages'))
    const publishedRequest = await published.requested
    await page.getByRole('button', { name: 'held/', exact: true }).click()
    const request = await directory.requested
    const aborted = page.waitForEvent('requestfailed', { predicate: (failed) => failed === request })
    await page.getByRole('button', { name: 'Root', exact: true }).click()
    await aborted
    await expect(page.getByRole('button', { name: 'fresh.html', exact: true })).toBeVisible()
    expect(publishedRequest.failure()).toBeNull()
    await published.release()
    await directory.release()
    await expect(page.getByText('old-owner page', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'late.html', exact: true })).toHaveCount(0)
    await page.screenshot({ path: info.outputPath('pages-directory.png') })
  } finally { discovery.releaseAll() }
})

for (const tab of ['pages', 'playwright'] as const) {
  test(`${tab} departure aborts every tab query and a fresh visit renders discovery`, async ({ page }, info) => {
    const discovery = await prepareDiscovery(page)
    const keys = tab === 'pages' ? [discoveryKey('pages'), discoveryKey('files', 'workspace/')]
      : [discoveryKey('tools'), discoveryKey('files', '.crabbox'), discoveryKey('files', '.crabbox/captures')]
    const holds = keys.map((key) => discovery.hold(key))
    try {
      await page.goto(agentPath(tab))
      const requests = await Promise.all(holds.map((hold) => hold.requested))
      const aborted = requests.map((request) => page.waitForEvent('requestfailed', { predicate: (failed) => failed === request }))
      await navigateChat(page, '/app/agents')
      await Promise.all(aborted)
      await Promise.all(holds.map((hold) => hold.release()))
      await navigateChat(page, agentPath(tab))
      if (tab === 'pages') {
        await expect(page.getByText('old-owner page', { exact: true })).toBeVisible()
        await expect(page.getByRole('button', { name: 'site.html', exact: true })).toBeVisible()
      } else {
        await expect(page.getByText('old-owner browser tool', { exact: true })).toBeVisible()
        await expect(page.getByText('.crabbox/captures/old-owner-report.html', { exact: true })).toBeVisible()
        await expect(page.getByText('.crabbox/old-owner-fallback.txt', { exact: true })).toHaveCount(0)
      }
      for (const key of keys) expect(discovery.requests.filter((request) => request.key === key)).toHaveLength(2)
      await page.screenshot({ path: info.outputPath(`${tab}-fresh.png`) })
    } finally { discovery.releaseAll() }
  })

  test(`${tab} replacement retires pending discovery without old-owner cache leakage`, async ({ page }) => {
    const discovery = await prepareDiscovery(page)
    const keys = tab === 'pages' ? [discoveryKey('pages'), discoveryKey('files', 'workspace/')]
      : [discoveryKey('tools'), discoveryKey('files', '.crabbox'), discoveryKey('files', '.crabbox/captures')]
    const holds = keys.map((key) => discovery.hold(key))
    try {
      await page.goto(agentPath(tab))
      const requests = await Promise.all(holds.map((hold) => hold.requested))
      const aborted = requests.map((request) => page.waitForEvent('requestfailed', { predicate: (failed) => failed === request }))
      await writeFromOtherTab(page, replacementToken)
      await Promise.all(aborted)
      await Promise.all(holds.map((hold) => hold.release()))
      await navigateChat(page, agentPath(tab, replacementAgent.id))
      await expect(page.getByText(tab === 'pages' ? 'current-owner page' : 'current-owner browser tool', { exact: true })).toBeVisible()
      await expect(page.getByText(/old-owner/)).toHaveCount(0)
    } finally { discovery.releaseAll() }
  })
}
