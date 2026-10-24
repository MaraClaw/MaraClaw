import assert from 'node:assert/strict'
import { expect as playwrightExpect } from '@playwright/test'

import { actorId, event, flush, tenantId } from './admin-audit-browser-harness.mjs'

const expect = playwrightExpect.configure({ timeout: 15_000 })
const firstCursor = 'opaque+/=&page=2'
const secondCursor = 'opaque-page-3'

export async function runPagingScenarios({ scenario, capture }) {
  for (const role of ['platform_admin', 'org_admin']) for (const mobile of [true, false]) for (const scheme of ['light', 'dark']) {
    await scenario({ name: `${role} ${mobile ? 'phone' : 'desktop'} ${scheme} older back refresh`, mobile, scheme, nextCursor: firstCursor, user: { role, is_platform_admin: role === 'platform_admin' } }, async (page, state) => {
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      state.result = [{ ...event, action: 'page.two', created_at: '2026-10-21T12:30:00.123456+08:00' }]
      state.nextCursor = secondCursor
      await page.getByRole('button', { name: 'Older', exact: true }).focus()
      await page.getByRole('button', { name: 'Older', exact: true }).press('Enter')
      await expect(page.getByRole('heading', { name: 'page.two', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeFocused()
      assert.equal(state.exchanges.at(-1).params.cursor, firstCursor)
      await expect(page.getByRole('heading', { name: event.action, exact: true })).toHaveCount(0)
      await expect(page.locator('time')).toHaveAttribute('datetime', '2026-10-21T12:30:00.123456+08:00')
      await page.locator('summary').press('Enter')
      await expect(page.getByText('2026-10-21T12:30:00.123456+08:00', { exact: true })).toBeVisible()
      await capture(page, `paging-${role}-${mobile}-${scheme}-older`)
      state.result = [{ ...event, action: 'page.three' }]; state.nextCursor = null
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'page.three', exact: true })).toBeVisible()
      assert.equal(state.exchanges.at(-1).params.cursor, secondCursor)
      await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeDisabled()
      const exhaustedCount = state.exchanges.length
      await page.getByRole('button', { name: 'Older', exact: true }).press('Enter')
      await flush(page)
      assert.equal(state.exchanges.length, exhaustedCount)
      await expect(page.getByRole('status')).toContainText('Page 3')
      await page.getByRole('button', { name: 'Back', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'page.two', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Back', exact: true }).focus()
      await page.getByRole('button', { name: 'Back', exact: true }).press('Enter')
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeFocused()
      const rootCount = state.exchanges.length
      await page.getByRole('button', { name: 'Back', exact: true }).press('Enter')
      await flush(page)
      assert.equal(state.exchanges.length, rootCount)
      state.result = [{ ...event, action: 'new.root' }]; state.nextCursor = 'fresh-page-2'
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'page.two', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'new.root', exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.at(-1).params, { limit: '100' })
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      state.result = [{ ...event, action: 'fresh.page.two' }]; state.nextCursor = null
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'fresh.page.two', exact: true })).toBeVisible()
      assert.equal(state.exchanges.at(-1).params.cursor, 'fresh-page-2')
      await page.getByRole('button', { name: 'Back', exact: true }).scrollIntoViewIfNeeded()
      await capture(page, `paging-${role}-${mobile}-${scheme}-end`)
    })
  }

  for (const reset of ['apply', 'clear']) await scenario({ name: `${reset} filters resets older page and preserves platform scope`, nextCursor: firstCursor, user: { role: 'platform_admin', is_platform_admin: true } }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    await page.getByLabel('Action', { exact: true }).fill('old.filter')
    await page.getByLabel('Tenant ID', { exact: true }).fill(tenantId)
    await page.getByLabel('Actor ID', { exact: true }).fill(actorId)
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    await expect.poll(() => state.exchanges.at(-1)?.params.action).toBe('old.filter')
    await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeEnabled()
    state.result = [{ ...event, action: 'older.filtered' }]
    await page.getByRole('button', { name: 'Older', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'older.filtered', exact: true })).toBeVisible()
    assert.deepEqual(state.exchanges.at(-1).params, { limit: '100', action: 'old.filter', cursor: firstCursor, tenant_id: tenantId, actor_id: actorId })
    state.result = [{ ...event, action: 'reset.root' }]; state.nextCursor = null
    await page.getByLabel('Action', { exact: true }).fill('new.filter')
    await page.getByRole('button', { name: reset === 'apply' ? 'Apply filters' : 'Clear filters', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
    if (reset === 'clear') await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'reset.root', exact: true })).toBeVisible()
    assert.deepEqual(state.exchanges.at(-1).params, reset === 'apply' ? { limit: '100', action: 'new.filter', tenant_id: tenantId, actor_id: actorId } : { limit: '100' })
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
    await expect(page.getByRole('heading', { name: 'older.filtered', exact: true })).toHaveCount(0)
  })

  for (const result of ['empty', 'invalid-cursor', 'malformed']) await scenario({ name: `older page ${result} offers back and root recovery`, nextCursor: firstCursor }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    state.result = result === 'malformed' ? [{ ...event, id: 'bad' }] : []
    state.status = result === 'invalid-cursor' ? 400 : 200; state.nextCursor = null
    await page.getByRole('button', { name: 'Older', exact: true }).click()
    if (result === 'empty') await expect(page.getByRole('heading', { name: '0 recent events' })).toBeVisible()
    else await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeDisabled()
    await capture(page, `paging-recovery-${result}`)
    state.status = 200; state.result = [{ ...event, action: 'recovered.root' }]
    await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'recovered.root', exact: true })).toBeVisible()
    assert.deepEqual(state.exchanges.at(-1).params, { limit: '100' })
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
  })

  for (const reset of ['filter', 'back', 'refresh']) for (const status of [200, 403]) await scenario({ name: `${reset} abandons late older-page ${status}`, nextCursor: firstCursor }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    const count = state.exchanges.length
    state.gate = Promise.withResolvers(); state.status = status; state.result = [{ ...event, action: 'retired.older' }]
    await page.getByRole('button', { name: 'Older', exact: true }).focus()
    await page.getByRole('button', { name: 'Older', exact: true }).press('Enter')
    await expect.poll(() => state.exchanges.length).toBe(count + 1)
    const pending = state.exchanges.at(-1)
    await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeFocused()
    await page.getByRole('button', { name: 'Older', exact: true }).press('Enter')
    await flush(page)
    assert.equal(state.exchanges.length, count + 1)
    await expect(page.getByRole('heading', { name: event.action, exact: true })).toHaveCount(0)
    state.gate = null; state.status = 200; state.result = [{ ...event, action: 'current.root' }]; state.nextCursor = null
    if (reset === 'filter') {
      await page.getByLabel('Action', { exact: true }).fill('new.filter')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    } else await page.getByRole('button', { name: reset === 'back' ? 'Back' : 'Refresh activity', exact: true }).click()
    await expect(page.getByRole('heading', { name: reset === 'back' ? event.action : 'current.root', exact: true })).toBeVisible()
    pending.gate.resolve(); await pending.delivered.promise; await flush(page)
    await expect(page.getByRole('heading', { name: 'retired.older', exact: true })).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
  })
}
