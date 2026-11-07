// Run: BROADCAST_ARTIFACTS=<owned evidence directory> node tests/broadcast-history.browser.mjs
// BROADCAST_HISTORY_BEFORE=1 captures the unchanged compose surface before implementation.
import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { admin, broadcastId, compose, createBroadcastHarness, flush } from './broadcast-browser-harness.mjs'

const harness = await createBroadcastHarness()
const { scenario, capture } = harness
const before = process.env.BROADCAST_HISTORY_BEFORE === '1'
const recent = {
  broadcast_id: broadcastId, sender_id: '18e275bd-29a0-4adf-a1ab-1cbf787798fb', sender_name: 'Alex Publisher',
  title: 'Maintenance window', body: 'Friday maintenance starts at 18:00.\nSave your work before the window.',
  send_email: true, published_at: '2026-11-06T12:30:00+08:00', users_notified: 7, agents_notified: 3, emails_sent: 5,
}
const older = { ...recent, broadcast_id: '18e275bd-29a0-4adf-a1ab-1cbf787798fb', sender_id: null, sender_name: 'Former publisher', title: 'Earlier announcement', body: '', published_at: '2026-11-05T04:00:00Z', users_notified: 0, agents_notified: 0, emails_sent: 0 }
const historyRegion = (page) => page.getByRole('region', { name: 'Published broadcasts', exact: true })
let completed = false
try {
  for (const mobile of [false, true]) for (const scheme of ['light', 'dark']) {
    await scenario({ name: `${mobile ? 'mobile' : 'desktop'} ${scheme} ${before ? 'before' : 'history rendering and paging'}`, mobile, scheme }, async (page, state) => {
      const prefix = `${mobile ? 'mobile' : 'desktop'}-${scheme}`
      await expect(page.getByRole('heading', { name: 'Compose message' })).toBeVisible()
      if (before) {
        await capture(page, `${prefix}-top-before`)
        await page.getByRole('button', { name: 'Review broadcast', exact: true }).scrollIntoViewIfNeeded()
        await capture(page, `${prefix}-scrolled-before`)
        return
      }
      state.history = (cursor) => cursor === 'older/+=' ? { items: [older], next_cursor: null } : { items: [recent, { ...older, title: 'Organization update' }], next_cursor: 'older/+=' }
      const region = historyRegion(page)
      await region.getByRole('button', { name: 'Refresh history' }).click()
      await expect(region.getByRole('heading', { name: recent.title })).toBeVisible()
      await page.getByLabel('Title', { exact: true }).fill('Keep draft')
      await page.getByLabel('Body (optional)').fill('Keep this body')
      await page.locator('#main').evaluate((main) => { main.scrollTop = 0 })
      await capture(page, `${prefix}-top-after`)
      await region.getByRole('heading', { name: recent.title }).scrollIntoViewIfNeeded()
      await expect(region.getByText(recent.body)).toHaveText(recent.body)
      await expect(region.getByText('Alex Publisher', { exact: true })).toBeVisible()
      await expect(region.getByText('Former publisher', { exact: true })).toBeVisible()
      const time = region.locator('time').first()
      await expect(time).toHaveText(await page.evaluate((date) => new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(date)), recent.published_at))
      await expect(time).toHaveAttribute('datetime', recent.published_at)
      assert.deepEqual(await region.locator('h3').allTextContents(), [recent.title, 'Organization update'])
      for (const [label, count] of [['Users notified', '7'], ['Agents notified', '3'], ['Email targets queued', '5']]) {
        await expect(region.locator('dl > div').filter({ has: page.getByText(label, { exact: true }) }).first().locator('dd')).toHaveText(count)
      }
      await region.evaluate((element) => {
        const main = document.querySelector('#main')
        main.scrollTop += element.getBoundingClientRect().top - main.getBoundingClientRect().top - 16
      })
      await capture(page, `${prefix}-history-after`)
      await region.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(region.getByRole('heading', { name: older.title })).toBeVisible()
      await expect(region.getByRole('button', { name: 'Older', exact: true })).toBeDisabled()
      assert.equal(state.historyRequests.at(-1).cursor, 'older/+=')
      assert.equal(state.historyRequests.at(-1).limit, '20')
      await region.getByRole('button', { name: 'Back', exact: true }).press('Enter')
      await expect(region.getByRole('heading', { name: recent.title })).toBeVisible()
      await region.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(region.getByRole('heading', { name: older.title })).toBeVisible()
      await region.getByRole('button', { name: 'Refresh history' }).click()
      await expect(region.getByRole('heading', { name: recent.title })).toBeVisible()
      await expect(region.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      assert.equal(state.historyRequests.at(-1).cursor, null)
      await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Keep draft')
      await expect(page.getByLabel('Body (optional)')).toHaveValue('Keep this body')
      await region.getByRole('button', { name: 'Older', exact: true }).scrollIntoViewIfNeeded()
      await capture(page, `${prefix}-scrolled-after`)
    })
  }
  if (!before) {
    await scenario({ name: 'loading empty failure retry and newly sent history preserve review', mobile: true }, async (page, state) => {
      const region = historyRegion(page)
      await expect(region).toContainText('after history tracking was enabled')
      await compose(page, { title: 'New publication', body: 'Published from this browser', send_email: true })
      state.historyGate = Promise.withResolvers()
      await region.getByRole('button', { name: 'Refresh history' }).click()
      await expect(region.getByRole('status')).toContainText('Loading')
      await capture(page, 'mobile-loading-after')
      await expect(page.getByRole('heading', { name: 'Review broadcast', exact: true })).toBeVisible()
      state.historyGate.resolve()
      await expect(region.getByRole('status')).not.toContainText('Loading')
      state.historyGate = null; state.historyStatus = 503
      await region.getByRole('button', { name: 'Refresh history' }).click()
      await expect(region.getByRole('alert')).toContainText('Synthetic service unavailable')
      await capture(page, 'mobile-error-after')
      state.historyStatus = 200; state.history = { items: [recent], next_cursor: 'older/+=' }
      await region.getByRole('button', { name: 'Retry history' }).click()
      await expect(region.getByRole('heading', { name: recent.title })).toBeVisible()
      await region.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(region.getByText('Page 2', { exact: true })).toBeVisible()
      state.history = { items: [{ ...recent, title: 'New publication', body: 'Published from this browser' }, { ...recent, broadcast_id: older.broadcast_id }], next_cursor: null }
      await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Broadcast sent', exact: true })).toBeVisible()
      await expect(region.getByRole('heading', { name: 'New publication', exact: true })).toBeVisible()
      await expect(region.getByText('Page 1', { exact: true })).toBeVisible()
      await expect(page.getByRole('region', { name: 'Email progress', exact: true })).toBeVisible()
      assert.equal(state.historyRequests.at(-1).cursor, null)
      await capture(page, 'mobile-new-publication-after')
      state.history = { items: [], next_cursor: null }
      await region.getByRole('button', { name: 'Refresh history' }).click()
      await expect(region.getByText('No published broadcasts yet.', { exact: true })).toBeVisible()
      await capture(page, 'mobile-empty-after')
    })
    await scenario({ name: 'retired history response cannot appear in replacement organization' }, async (page, state) => {
      const region = historyRegion(page)
      await expect(region.getByRole('button', { name: 'Refresh history' })).toBeEnabled()
      state.history = { items: [{ ...recent, title: 'Retired organization secret' }], next_cursor: null }
      state.historyGate = Promise.withResolvers()
      const previousRequests = state.historyRequests.length
      await region.getByRole('button', { name: 'Refresh history' }).click()
      await expect.poll(() => state.historyRequests.length).toBe(previousRequests + 1)
      const retired = state.historyRequests.at(-1)
      await page.getByRole('button', { name: 'Sign out', exact: true }).click()
      state.user = { ...admin, id: '28e275bd-29a0-4adf-a1ab-1cbf787798fb', tenant_id: '28e275bd-29a0-4adf-a1ab-1cbf787798fd' }; state.token = 'replacement-synthetic'
      state.history = { items: [{ ...recent, title: 'Replacement organization update' }], next_cursor: null }; state.historyGate = null
      await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
      await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
      await page.getByRole('button', { name: 'Sign in', exact: true }).click()
      await expect(region.getByRole('heading', { name: 'Replacement organization update' })).toBeVisible()
      retired.gate.resolve(); await retired.delivered.promise; await flush(page)
      await expect(page.getByText('Retired organization secret', { exact: true })).toHaveCount(0)
      await capture(page, 'replacement-isolation-after')
    })
  }
  completed = true
} finally { await harness.close(completed) }
