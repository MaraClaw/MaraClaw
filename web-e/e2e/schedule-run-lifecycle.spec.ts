import { expect, test } from '@playwright/test'

import { manualAcceptance, prepareRunSchedules, receiptPath, runPath, runReceipt } from './schedule-run-fixture'
import { savedSchedule, schedulesPath } from './schedules-fixture'
import { delayedAgent, originalToken, replacementToken, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

for (const departure of ['session', 'unmount', 'agent', 'superseded'] as const) {
  for (const operation of ['status', 'cancel-success', 'cancel-conflict'] as const) {
    test(`late receipt ${operation} is fenced after ${departure}`, async ({ page }) => {
      // Given
      await prepareRunSchedules(page)
      await page.clock.install()
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      const delivered = Promise.withResolvers<void>()
      const nextId = 'next-owned-run'
      let acceptances = 0
      let oldReads = 0
      let nextReads = 0
      await page.route(`**${runPath}`, (route) => route.fulfill({ json: {
        ...manualAcceptance, run_id: ++acceptances === 1 ? manualAcceptance.run_id : nextId,
      } }))
      await page.route(`**${receiptPath.replace(manualAcceptance.run_id, nextId)}`, (route) => {
        nextReads++
        return route.fulfill({ json: runReceipt('success', nextId) })
      })
      await page.route(`**${receiptPath}`, async (route) => {
        oldReads++
        expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
        if (operation !== 'status') return route.fulfill({ json: runReceipt() })
        requested.resolve()
        await release.promise
        await route.fulfill({ json: runReceipt('failure') })
        delivered.resolve()
      })
      if (operation !== 'status') await page.route(`**${receiptPath}/cancel`, async (route) => {
        requested.resolve()
        await release.promise
        await route.fulfill(operation === 'cancel-conflict'
          ? { status: 409, json: { detail: 'Old run started' } }
          : { json: runReceipt('cancelled') })
        delivered.resolve()
      })
      const nextSchedules = `/app/agents/${delayedAgent.id}/schedules`
      await page.route(`**/api/agents/${delayedAgent.id}/schedules/**`, (route) => {
        const path = new URL(route.request().url()).pathname
        if (path.endsWith('/run')) return route.fulfill({ json: { ...manualAcceptance, run_id: nextId } })
        if (path.includes('/runs/')) {
          nextReads++
          return route.fulfill({ json: { ...runReceipt('success', nextId), agent_id: delayedAgent.id } })
        }
        return route.fulfill({ json: [savedSchedule] })
      })
      await page.goto(schedulesPath)
      await page.getByRole('button', { name: 'Run', exact: true }).click()
      if (operation !== 'status') await page.getByRole('button', { name: 'Cancel pending run' }).click()
      await requested.promise
      switch (departure) {
        case 'session':
          await writeFromOtherTab(page, replacementToken)
          await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
          break
        case 'unmount':
          await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
          break
        case 'agent':
          await page.evaluate((path) => { history.pushState({}, '', path); dispatchEvent(new PopStateEvent('popstate')) }, nextSchedules)
          await page.getByRole('button', { name: 'Run', exact: true }).click()
          break
        case 'superseded':
          await page.getByRole('button', { name: 'Run', exact: true }).click()
          break
        default: departure satisfies never
      }
      if (departure === 'agent' || departure === 'superseded') {
        await expect(page.getByRole('region', { name: 'Current run' })).toContainText(nextId)
        await expect(page.getByText('Success', { exact: true })).toBeVisible()
      } else {
        await expect(page).toHaveURL('/app/settings')
        await expect(page.getByRole('button', { name: 'Run', exact: true })).toHaveCount(0)
      }
      // When
      release.resolve()
      await delivered.promise
      await page.clock.runFor(6000)
      // Then
      expect(oldReads).toBe(1)
      if (departure === 'agent' || departure === 'superseded') {
        expect(nextReads).toBe(1)
        await expect(page.getByRole('region', { name: 'Current run' })).toContainText(nextId)
        await expect(page.getByText('Success', { exact: true })).toBeVisible()
      } else await expect(page.getByRole('region', { name: 'Current run' })).toHaveCount(0)
      await expect(page.getByText(/Cancellation lost the race|Old run started|Cancelled|Failure/, { exact: true })).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(departure === 'session' ? replacementToken : originalToken)
    })
  }
}

for (const departure of ['unmount', 'agent'] as const) {
  test(`late trigger acceptance cannot start a status query after ${departure}`, async ({ page }) => {
    // Given
    await prepareRunSchedules(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    let reads = 0
    await page.route(`**${receiptPath}`, (route) => { reads++; return route.fulfill({ json: runReceipt() }) })
    await page.route(`**${runPath}`, async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ json: manualAcceptance })
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Run', exact: true }).click()
    await requested.promise
    if (departure === 'unmount') await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
    else await page.evaluate((path) => { history.pushState({}, '', path); dispatchEvent(new PopStateEvent('popstate')) }, `/app/agents/${delayedAgent.id}/settings`)
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toHaveCount(0)
    const finished = page.waitForEvent('requestfinished', { predicate: (request) => request.url().endsWith(runPath) })
    // When
    release.resolve()
    await finished
    // Then
    await expect(page.getByText('Run accepted. Check history for completion.')).toHaveCount(0)
    expect(reads).toBe(0)
  })
}
