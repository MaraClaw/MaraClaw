import assert from 'node:assert/strict'
import { expect as playwrightExpect } from '@playwright/test'

import { event, flush } from './admin-audit-browser-harness.mjs'

const expect = playwrightExpect.configure({ timeout: 15_000 })

export async function runTargetScenarios({ scenario, capture }) {
  for (const role of ['platform_admin', 'org_admin']) for (const mobile of [true, false]) for (const scheme of ['light', 'dark']) {
    await scenario({ name: `target filter ${role} ${mobile} ${scheme}`, mobile, scheme, user: { role, is_platform_admin: role === 'platform_admin' } }, async (page, state) => {
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      const target = " Tenant%_'&limit=0 "
      const input = page.getByLabel('Target type', { exact: true })
      const initialCount = state.exchanges.length
      await input.fill(target)
      await flush(page)
      assert.equal(state.exchanges.length, initialCount)
      state.result = [{ ...event, action: 'target.root', target_type: target }]; state.nextCursor = 'target-page-2'
      const applied = page.waitForRequest((request) => new URL(request.url()).searchParams.get('target_type') === target)
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await applied
      await expect(page.getByRole('heading', { name: 'target.root', exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.at(-1).params, { limit: '100', target_type: target })
      state.result = [{ ...event, action: 'target.older', target_type: target }]; state.nextCursor = 'target-page-3'
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'target.older', exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.at(-1).params, { limit: '100', target_type: target, cursor: 'target-page-2' })
      await page.getByRole('button', { name: 'Back', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'target.root', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'target.older', exact: true })).toBeVisible()
      state.result = [{ ...event, action: 'target.changed', target_type: 'user' }]; state.nextCursor = 'user-page-2'
      await input.fill('user')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'target.changed', exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.at(-1).params, { limit: '100', target_type: 'user' })
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      await expect(page.getByRole('status')).toContainText('Page 1')
      await input.focus()
      await expect(input).toBeFocused()
      await capture(page, `target-${role}-${mobile}-${scheme}-changed-focus`)
      state.result = [{ ...event, action: 'user.older' }]
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'user.older', exact: true })).toBeVisible()
      assert.equal(state.exchanges.at(-1).params.cursor, 'user-page-2')
      await page.getByRole('button', { name: 'Clear filters', exact: true }).click()
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      await expect(input).toHaveValue('')
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      await expect(page.getByRole('status')).toContainText('Page 1')
      state.result = [{ ...event, action: 'cleared.root' }]; state.nextCursor = null
      await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'cleared.root', exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.at(-1).params, { limit: '100' })
      await expect(page.getByRole('heading', { name: 'target.older', exact: true })).toHaveCount(0)
      await capture(page, `target-${role}-${mobile}-${scheme}-cleared`)
    })
  }
}
