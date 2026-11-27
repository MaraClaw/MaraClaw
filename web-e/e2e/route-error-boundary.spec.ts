import { expect, test, type Page } from '@playwright/test'

import { activity, activityApiPath, activityPath, prepareActivity } from './activity-fixture'
import { originalAgent } from './session-fencing-fixture'

const RELOAD_KEY = 'maraclaw-chunk-reload'
const DOWNLOAD_FAILED = 'This page couldn’t be downloaded'

/** Aborts every request for one route chunk until `restore`; `downloads()` counts the attempts. */
async function blockChunk(page: Page, chunk: RegExp) {
  let downloads = 0
  await page.route(chunk, (route) => {
    downloads++
    return route.abort()
  })
  return { downloads: () => downloads, restore: () => page.unroute(chunk) }
}

function documentRequests(page: Page) {
  const paths: string[] = []
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) paths.push(new URL(request.url()).pathname)
  })
  return paths
}

test('a missing agent tab chunk keeps the workspace and agent navigation, and Try again downloads it without a reload', async ({ page }, testInfo) => {
  // Given a manager whose Activity tab chunk is gone, as after a deploy
  await prepareActivity(page)
  await page.route(`**${activityApiPath}*`, (route) => route.fulfill({ json: { items: [activity], next_cursor: null } }))
  const chunk = await blockChunk(page, /\/assets\/agent-activity-[\w-]+\.js$/)
  const documents = documentRequests(page)
  // When they open the tab, the stale-deploy handler reloads once and the route boundary takes over
  await page.goto(activityPath)
  const alert = page.getByRole('alert')
  await expect(alert).toContainText(DOWNLOAD_FAILED)
  await expect(alert).toContainText('Check your connection and try again.')
  await expect(page.getByRole('navigation', { name: 'Agent sections' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Workspace' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible()
  expect(documents).toEqual([activityPath, activityPath])
  expect(await page.evaluate((key) => sessionStorage.getItem(key), RELOAD_KEY)).toBe(page.url())
  await page.screenshot({ path: testInfo.outputPath('agent-tab-download-failed.png') })

  // Then Try again downloads the chunk afresh and, while it is still missing, shows the same message without reloading
  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect.poll(chunk.downloads).toBe(3)
  await expect(page.getByRole('button', { name: 'Try again', exact: true })).toBeVisible()
  await expect(alert).toContainText(DOWNLOAD_FAILED)

  // And another route renders through the workspace navigation, clearing the failure
  await page.getByRole('navigation', { name: 'Workspace' }).getByRole('link', { name: 'Agents', exact: true }).click()
  await expect(page).toHaveURL('/app/agents')
  await expect(page.getByText(originalAgent.name, { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)

  // And returning to the tab downloads it again instead of replaying the old failure
  await page.goBack()
  await expect(page).toHaveURL(activityPath)
  await expect.poll(chunk.downloads).toBe(4)
  await expect(alert).toContainText(DOWNLOAD_FAILED)

  // When the chunk is reachable again, Try again renders the tab in place
  await chunk.restore()
  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: 'Agent sections' })).toBeVisible()
  expect(documents).toEqual([activityPath, activityPath])
})

test('a missing workspace page chunk keeps the shell and its navigation, and Try again loads the page', async ({ page }) => {
  // Given a member whose agent list chunk is gone
  await prepareActivity(page)
  const chunk = await blockChunk(page, /\/assets\/agents-list-[\w-]+\.js$/)
  const documents = documentRequests(page)
  // When they open the workspace, the shell stays and only the page area reports the failure
  await page.goto('/app/agents')
  await expect(page.getByRole('alert')).toContainText(DOWNLOAD_FAILED)
  const workspace = page.getByRole('navigation', { name: 'Workspace' })
  await expect(workspace.getByRole('link', { name: 'Agents', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible()
  expect(documents).toEqual(['/app/agents', '/app/agents'])
  // Then Try again loads the page once the chunk is back, with no further reload
  await chunk.restore()
  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect(page.getByText(originalAgent.name, { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(chunk.downloads()).toBe(2)
  expect(documents).toEqual(['/app/agents', '/app/agents'])
})
