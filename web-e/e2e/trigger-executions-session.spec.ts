import { expect, test } from '@playwright/test'

import { originalToken, tokenKey } from './session-fencing-fixture'
import { historyExecution, historyPath, olderId, prepareTriggerHistory, primaryTrigger, tasksPath } from './trigger-executions-fixture'

for (const status of [200, 401, 503]) {
  test(`same-token session retirement clears history and fences delayed page ${status}`, async ({ page }) => {
    // Given
    await prepareTriggerHistory(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const handled = Promise.withResolvers<void>()
    const cursors: (string | null)[] = []
    let replaced = false
    await page.route(`**${historyPath}*`, async (route) => {
      const cursor = new URL(route.request().url()).searchParams.get('cursor')
      cursors.push(cursor)
      if (replaced) return route.fulfill({ json: { items: [historyExecution(olderId)], next_cursor: null } })
      if (cursor === null) return route.fulfill({ json: { items: [historyExecution()], next_cursor: 'retiring-page' } })
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? { items: [historyExecution()], next_cursor: null } : { detail: 'obsolete private error' } })
      handled.resolve()
    })
    await page.goto(tasksPath)
    await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
    const history = page.getByRole('region', { name: `${primaryTrigger.name} execution history` })
    await history.getByRole('button', { name: 'Load older executions', exact: true }).click()
    await requested.promise
    const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('cursor=retiring-page') })
    const peer = await page.context().newPage()
    try {
      await peer.route('**/trigger-history-peer', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Trigger history session peer</title>' }))
      await peer.goto('/trigger-history-peer')
      // When
      await peer.evaluate(({ tokenKey, originalToken }) => {
        localStorage.removeItem(tokenKey)
        localStorage.setItem(tokenKey, originalToken)
      }, { tokenKey, originalToken })
      // Then
      await cancelled
      await expect(history).toHaveCount(0)
      replaced = true
      release.resolve()
      await handled.promise
      await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
      await expect(history.getByRole('listitem')).toHaveCount(1)
      await history.locator('summary').click()
      await expect(history.getByText(olderId, { exact: true })).toBeVisible()
      await expect(history.getByRole('alert')).toHaveCount(0)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      expect(cursors).toEqual([null, 'retiring-page', null])
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
    } finally {
      release.resolve()
      await peer.close()
    }
  })
}
