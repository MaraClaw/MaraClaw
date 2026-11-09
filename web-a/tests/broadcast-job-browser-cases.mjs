import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { acceptance, admin, completedJob, compose, flush, metric } from './broadcast-browser-harness.mjs'

export async function runBroadcastJobCases({ scenario, capture }) {
  for (const mobile of [false, true]) for (const scheme of ['light', 'dark']) {
    const prefix = `job-${mobile ? 'mobile' : 'desktop'}-${scheme}`
    await scenario({ name: `${prefix} acceptance, committed progress, status recovery and single completion`, mobile, scheme }, async (page, state) => {
      await page.clock.install()
      state.jobGate = Promise.withResolvers()
      state.job = { ...completedJob, status: 'pending', completed_at: null, processed: 0, users_notified: 0, agents_notified: 0, skipped: 0, emails_queued: 0 }
      await expect(page.getByRole('button', { name: 'Refresh history', exact: true })).toBeEnabled()
      const historyRequests = state.historyRequests.length
      await compose(page)
      await page.getByRole('button', { name: 'Send broadcast', exact: true }).press('Enter')
      const progress = page.getByRole('region', { name: 'Broadcast accepted', exact: true })
      await expect(progress.getByRole('heading')).toBeFocused()
      await expect.poll(() => state.jobRequests.length).toBeGreaterThanOrEqual(1)
      await metric(progress, 'Captured users', acceptance.users_total)
      await metric(progress, 'Captured agents', acceptance.agents_total)
      await expect(page.getByRole('textbox')).toHaveCount(0)
      assert.equal(state.emailRequests.length, 0)
      assert.equal(state.historyRequests.length, historyRequests)
      await capture(page, `${prefix}-accepted`)
      state.jobGate.resolve(); await state.jobRequests[0].delivered.promise
      const refresh = page.getByRole('button', { name: 'Refresh broadcast progress', exact: true })
      await expect(refresh).toBeEnabled()
      state.jobGate = null
      state.job = { ...completedJob, status: 'processing', completed_at: null, processed: 5, users_notified: 3, agents_notified: 1, skipped: 1, emails_queued: 2 }
      await refresh.click()
      const running = page.getByRole('region', { name: 'Broadcast in progress', exact: true })
      await metric(running, 'Recipients processed', 5)
      await metric(running, 'Skipped targets', 1)
      await metric(running, 'Queued email targets', 2)
      await capture(page, `${prefix}-progress`)
      state.jobHttpStatus = 503
      await refresh.click()
      await expect(running.getByRole('alert')).toContainText('The accepted job is kept')
      await metric(running, 'Recipients processed', 5)
      await capture(page, `${prefix}-status-error`)
      const requests = state.jobRequests.length
      await page.clock.fastForward(60_000)
      assert.equal(state.jobRequests.length, requests)
      assert.equal(state.exchanges.length, 1)
      assert.equal(state.emailRequests.length, 0)
      assert.equal(state.historyRequests.length, historyRequests)
      state.jobHttpStatus = 200
      state.job = { ...completedJob, users_notified: 6, skipped: 1 }
      await refresh.press('Enter')
      await expect(page.getByText('Notification processing completed.', { exact: true })).toBeVisible()
      await expect(page.getByRole('region', { name: 'Email progress', exact: true })).toBeVisible()
      await expect.poll(() => state.historyRequests.length).toBe(historyRequests + 1)
      await expect.poll(() => state.emailRequests.length).toBe(1)
      await expect(page.getByRole('status').filter({ has: page.getByRole('heading', { name: 'Broadcast sent', exact: true }) })).toBeFocused()
      await capture(page, `${prefix}-completed`)
      const completedRequests = state.jobRequests.length
      await page.clock.fastForward(10_000)
      await flush(page)
      assert.equal(state.jobRequests.length, completedRequests)
      assert.equal(state.historyRequests.length, historyRequests + 1)
      assert.equal(state.exchanges.length, 1)
    })
  }

  for (const mobile of [false, true]) for (const sameToken of [false, true]) for (const code of [200, 503]) {
    const prefix = `job-session-${mobile ? 'mobile' : 'desktop'}-${sameToken ? 'same-token' : 'different-token'}-${code}`
    await scenario({ name: `${prefix} retired status cannot complete or alter replacement draft`, mobile }, async (page, state) => {
      await page.clock.install()
      state.jobGate = Promise.withResolvers(); state.jobHttpStatus = code
      await compose(page)
      await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Broadcast accepted', exact: true })).toBeVisible()
      await expect.poll(() => state.jobRequests.length).toBeGreaterThanOrEqual(1)
      const retired = state.jobRequests[0]
      const jobRequests = state.jobRequests.length
      state.user = { ...admin, id: '28e275bd-29a0-4adf-a1ab-1cbf787798fb', tenant_id: '28e275bd-29a0-4adf-a1ab-1cbf787798fd' }
      if (!sameToken) state.token = 'synthetic-next-token'
      await page.getByRole('button', { name: 'Sign out', exact: true }).click()
      await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
      await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
      await page.getByRole('button', { name: 'Sign in', exact: true }).click()
      await page.getByLabel('Title', { exact: true }).fill('Replacement draft')
      await page.getByLabel('Body (optional)').fill('Retain this new message')
      await expect(page.getByRole('button', { name: 'Refresh history', exact: true })).toBeEnabled()
      const historyRequests = state.historyRequests.length
      retired.gate.resolve(); await retired.delivered.promise; await flush(page)
      await page.clock.fastForward(60_000)
      await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Replacement draft')
      await expect(page.getByLabel('Body (optional)')).toHaveValue('Retain this new message')
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.getByRole('heading', { name: /Broadcast (accepted|in progress|sent)/ })).toHaveCount(0)
      assert.equal(state.exchanges.length, 1)
      assert.equal(state.jobRequests.length, jobRequests)
      assert.equal(state.emailRequests.length, 0)
      assert.equal(state.historyRequests.length, historyRequests)
      await capture(page, prefix)
    })
  }

  await scenario({ name: '409 acceptance conflict requires review and a fresh key' }, async (page, state) => {
    state.status = 409
    await compose(page)
    await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('request key was already used')
    await compose(page)
    state.status = 200
    await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Broadcast sent', exact: true })).toBeVisible()
    assert.equal(state.exchanges.length, 2)
    assert.notEqual(state.exchanges[0].key, state.exchanges[1].key)
  })
}
