import { expect, test } from '@playwright/test'

import { activity, activityApiPath, activityPath, prepareActivity } from './activity-fixture'
import { originalAgent, originalToken, tokenKey } from './session-fencing-fixture'

for (const status of [200, 401, 503]) {
  test(`retired same-token Activity page ${status} cannot affect replacement history or authentication`, async ({ page }) => {
    // Given
    await prepareActivity(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const handled = Promise.withResolvers<void>()
    let replaced = false
    const cursors: (string | null)[] = []
    await page.route(`**${activityApiPath}*`, async (route) => {
      const cursor = new URL(route.request().url()).searchParams.get('cursor')
      cursors.push(cursor)
      if (replaced) return route.fulfill({ json: { items: [{ ...activity, summary: 'Replacement history' }], next_cursor: null } })
      if (!cursor) return route.fulfill({ json: { items: [activity], next_cursor: 'retiring-page' } })
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? { items: [{ ...activity, summary: 'Retired private action' }], next_cursor: null } : { detail: 'Retired private error' } })
      handled.resolve()
    })
    await page.goto(activityPath)
    await page.getByRole('button', { name: 'Load older activity', exact: true }).click()
    await requested.promise
    const peer = await page.context().newPage()
    try {
      await peer.route('**/activity-session-peer', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Activity session peer</title>' }))
      await peer.goto('/activity-session-peer')
      const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('cursor=retiring-page') })
      // When
      replaced = true
      await peer.evaluate(({ tokenKey, originalToken }) => {
        localStorage.removeItem(tokenKey)
        localStorage.setItem(tokenKey, originalToken)
      }, { tokenKey, originalToken })
      // Then
      await cancelled
      release.resolve()
      await handled.promise
      await expect(page.getByText('Replacement history', { exact: true })).toBeVisible()
      await expect(page.getByText(activity.summary, { exact: true })).toHaveCount(0)
      await expect(page.getByText('Retired private action', { exact: true })).toHaveCount(0)
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      expect(cursors).toEqual([null, 'retiring-page', null])
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
      await expect(page.getByRole('link', { name: 'Activity', exact: true })).toHaveAttribute('href', `/app/agents/${originalAgent.id}/activity`)
    } finally {
      release.resolve()
      await peer.close()
    }
  })
}
