import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { admin, confirmCancellation, flush, metric, sendReceipt } from './broadcast-browser-harness.mjs'

export async function runRetiredReceiptCases({ scenario, capture }) {
  for (const operation of ['status', 'cancel']) for (const retirement of ['route', 'same-token', 'different-token']) for (const code of [200, 503]) {
    await scenario({ name: `late email ${operation} ${code} after ${retirement} retirement cannot affect a new draft` }, async (page, state) => {
      // Given
      await page.clock.install()
      if (operation === 'status') { state.emailGate = Promise.withResolvers(); state.emailHttpStatus = code }
      const progress = await sendReceipt(page)
      if (operation === 'cancel') {
        await metric(progress, 'Queued', 5)
        state.cancelGate = Promise.withResolvers(); state.cancelHttpStatus = code
        await confirmCancellation(progress)
        await expect.poll(() => state.cancelRequests.length).toBe(1)
      } else await expect.poll(() => state.emailRequests.length).toBeGreaterThanOrEqual(1)
      const retired = operation === 'status' ? state.emailRequests.at(-1) : state.cancelRequests[0]
      assert.ok(retired)
      if (retirement === 'route') {
        await page.getByRole('link', { name: 'Account', exact: true }).click()
        await expect(page).toHaveURL(/\/account$/)
        await expect(page.getByLabel('Title', { exact: true })).toHaveCount(0)
        await page.getByRole('link', { name: 'Broadcast', exact: true }).click()
      } else {
        state.user = { ...admin, id: 'replacement', tenant_id: 'tenant-b' }
        if (retirement === 'different-token') state.token = 'synthetic-next-token'
        await page.getByRole('button', { name: 'Sign out', exact: true }).click()
        await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
        await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
        await page.getByRole('button', { name: 'Sign in', exact: true }).click()
      }
      await page.getByLabel('Title', { exact: true }).fill('New owner draft')
      await page.getByLabel('Body (optional)').fill('Keep my new content')
      const requests = state.emailRequests.length
      // When
      retired.gate.resolve(); await retired.delivered.promise; await flush(page)
      // Then
      await expect(page.getByLabel('Title', { exact: true })).toHaveValue('New owner draft')
      await expect(page.getByLabel('Body (optional)')).toHaveValue('Keep my new content')
      await expect(page.getByRole('region', { name: 'Email progress', exact: true })).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Broadcast sent' })).toHaveCount(0)
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.locator('[data-sonner-toast]').filter({ hasNotText: /^Signed in$/ })).toHaveCount(0)
      await page.clock.fastForward(60_000)
      assert.equal(state.emailRequests.length, requests)
      assert.equal(state.exchanges.length, 1)
      assert.equal(state.cancelRequests.length, operation === 'cancel' ? 1 : 0)
      await capture(page, `email-retired-${operation}-${retirement}-${code}`)
    })
  }
}
