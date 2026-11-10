import { expect, test } from '@playwright/test'
import { prepareHeldDashboardWrite, writes } from './dashboard-held-mutation-fixture'
import { currentPeriod, previousPeriod } from './dashboard-pages-fixture'
import { replacementAgent, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

for (const write of writes) {
  for (const departure of ['route', 'scope'] as const) {
    for (const returnTiming of ['before', 'after'] as const) {
      test(`${write} retires cards and continuation after ${departure} departure and return ${returnTiming} completion`, async ({ page }, testInfo) => {
        const held = await prepareHeldDashboardWrite(page, write)
        try {
          await page.goto(held.path)
          await expect(page.getByText(held.first, { exact: true })).toBeVisible()
          await held.submit()
          if (departure === 'scope' && held.isOkr) {
            await page.getByLabel('Period').selectOption(previousPeriod.start)
            await expect(page.getByText('Previous objective', { exact: true })).toBeVisible()
          } else if (departure === 'scope') {
            await page.evaluate((agentId) => {
              history.pushState(null, '', `/app/agents/${agentId}/relationships`)
              dispatchEvent(new PopStateEvent('popstate'))
            }, replacementAgent.id)
            await expect(page.getByText('Replacement person', { exact: true })).toBeVisible()
          } else {
            await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
            await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible()
          }
          if (returnTiming === 'after') await held.complete()
          if (departure === 'scope' && held.isOkr) await page.getByLabel('Period').selectOption(currentPeriod.start)
          else {
            // This is a client-side route transition; page.goto would discard the cache.
            await page.evaluate((path) => {
              history.pushState(null, '', path)
              dispatchEvent(new PopStateEvent('popstate'))
            }, held.path)
          }
          if (returnTiming === 'before') {
            await expect(page.getByText(held.first, { exact: true })).toBeVisible()
            await page.getByRole('navigation', { name: held.navigation }).getByRole('button', { name: 'Next' }).click()
            await expect(page.getByText(held.isOkr ? 'Second objective' : 'Unseen', { exact: true })).toBeVisible()
            await held.complete()
          }
          await held.expectUpdated()
          await page.getByRole('navigation', { name: held.navigation }).getByRole('button', { name: 'Previous' }).click()
          await expect(page.getByRole('navigation', { name: held.navigation }).getByRole('status')).toHaveText('Page 1')
          await page.screenshot({ path: testInfo.outputPath('held-write-return.png') })
          await testInfo.attach('held-write-requests', { body: JSON.stringify(held.requests), contentType: 'application/json' })
        } finally { held.release() }
      })
    }
  }

  test(`${write} held success cannot reset a replacement session's data or continuation`, async ({ page }, testInfo) => {
    const held = await prepareHeldDashboardWrite(page, write)
    try {
      await page.goto(held.path)
      await expect(page.getByText(held.first, { exact: true })).toBeVisible()
      await held.submit()
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByText(held.isOkr ? 'Replacement objective' : 'Replacement person', { exact: true })).toBeVisible()
      const nav = page.getByRole('navigation', { name: held.navigation })
      await nav.getByRole('button', { name: 'Next' }).click()
      await expect(page.getByText(held.isOkr ? 'Second objective' : 'Unseen', { exact: true })).toBeVisible()
      if (held.isOkr) await page.getByLabel('New company objective').fill('Replacement draft')
      else await page.getByLabel('Choose a person').selectOption('Added')
      const before = [...held.requests]
      await held.complete()
      await expect(nav.getByRole('status')).toHaveText('Page 2')
      await expect(page.getByText(held.isOkr ? 'Second objective' : 'Unseen', { exact: true })).toBeVisible()
      if (held.isOkr) await expect(page.getByLabel('New company objective')).toHaveValue('Replacement draft')
      else await expect(page.getByLabel('Choose a person')).toHaveValue('Added')
      await expect(page.getByText('Objective created', { exact: true })).toHaveCount(0)
      await expect(page.getByText('Relationship saved', { exact: true })).toHaveCount(0)
      expect(held.requests).toEqual(before)
      await nav.getByRole('button', { name: 'Previous' }).click()
      await expect(page.getByText(held.isOkr ? 'Replacement objective' : 'Replacement person', { exact: true })).toBeVisible()
      expect(held.requests).toEqual(before)
      await testInfo.attach('replacement-session-requests', { body: JSON.stringify(held.requests), contentType: 'application/json' })
    } finally { held.release() }
  })
}
