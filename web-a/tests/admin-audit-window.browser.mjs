import assert from 'node:assert/strict'
import { expect as playwrightExpect } from '@playwright/test'

import { actorId, event, flush, tenantId } from './admin-audit-browser-harness.mjs'

const expect = playwrightExpect.configure({ timeout: 15_000 })
const start = '2026-10-21T04:30:00.123456Z'
const end = '2026-10-22T04:30:00.123457Z'

export async function runWindowScenarios({ scenario, capture }) {
  for (const role of ['platform_admin', 'org_admin']) for (const mobile of [false, true]) for (const scheme of ['light', 'dark']) {
    await scenario({ name: `${role} ${mobile ? 'phone' : 'desktop'} ${scheme} applied UTC window navigation`, mobile, scheme, nextCursor: 'window-page-2', user: { role, is_platform_admin: role === 'platform_admin' } }, async (page, state) => {
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      const count = state.exchanges.length
      await page.getByLabel('Start time', { exact: true }).fill(' 2026-10-21T12:30:00.123456+08:00 ')
      await page.getByLabel('End time', { exact: true }).fill('2026-10-21T23:30:00.123457-05:00')
      await page.getByLabel('Action', { exact: true }).fill('user.activate')
      if (role === 'platform_admin') {
        await page.getByLabel('Tenant ID', { exact: true }).fill(tenantId)
        await page.getByLabel('Actor ID', { exact: true }).fill(actorId)
      }
      await flush(page)
      assert.equal(state.exchanges.length, count)
      const params = { limit: '100', action: 'user.activate', start_at: start, end_at: end, ...(role === 'platform_admin' ? { tenant_id: tenantId, actor_id: actorId } : {}) }
      state.result = [{ ...event, action: 'window.root' }]
      await page.getByRole('button', { name: 'Apply filters', exact: true }).press('Enter')
      await expect(page.getByRole('heading', { name: 'window.root', exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.at(-1).params, params)
      await expect(page.getByText(`Applied UTC window: ${start} (inclusive) to ${end} (exclusive).`, { exact: true })).toBeVisible()
      await capture(page, `window-${role}-${mobile}-${scheme}-applied`)

      await page.getByLabel('Start time', { exact: true }).fill('2026-10-21T00:00:00Z')
      state.result = [{ ...event, action: 'window.older' }]; state.nextCursor = null
      await page.getByRole('button', { name: 'Older', exact: true }).focus()
      await page.getByRole('button', { name: 'Older', exact: true }).press('Enter')
      await expect(page.getByRole('heading', { name: 'window.older', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeFocused()
      assert.deepEqual(state.exchanges.at(-1).params, { ...params, cursor: 'window-page-2' })
      await page.getByRole('button', { name: 'Back', exact: true }).press('Enter')
      await expect(page.getByRole('heading', { name: 'window.root', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
      await expect.poll(() => state.exchanges.at(-1)?.params.cursor).toBeUndefined()
      assert.deepEqual(state.exchanges.at(-1).params, params)

      state.nextCursor = 'equivalent-page-2'
      await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeEnabled()
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeEnabled()
      await page.getByLabel('Start time', { exact: true }).fill(start)
      await page.getByLabel('End time', { exact: true }).fill(end)
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      await expect(page.getByRole('status')).toContainText('Page 1')
      await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
      await expect.poll(() => state.exchanges.at(-1)?.params.cursor).toBeUndefined()
      assert.deepEqual(state.exchanges.at(-1).params, params)

      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeEnabled()
      await page.getByLabel('End time', { exact: true }).fill('2026-10-23T00:00:00Z')
      state.result = [{ ...event, action: 'new.window.root' }]
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'new.window.root', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      assert.deepEqual(state.exchanges.at(-1).params, { ...params, end_at: '2026-10-23T00:00:00.000000Z' })
      await page.getByLabel('Start time', { exact: true }).fill('')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect.poll(() => state.exchanges.at(-1)?.params.start_at).toBeUndefined()
      assert.equal(state.exchanges.at(-1).params.end_at, '2026-10-23T00:00:00.000000Z')
      await page.getByLabel('Start time', { exact: true }).fill(start)
      await page.getByLabel('End time', { exact: true }).fill('')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect.poll(() => state.exchanges.at(-1)?.params.end_at).toBeUndefined()
      assert.equal(state.exchanges.at(-1).params.start_at, start)
      await page.getByRole('button', { name: 'Clear filters', exact: true }).click()
      await expect(page.getByLabel('Start time', { exact: true })).toHaveValue('')
      await expect(page.getByLabel('End time', { exact: true })).toHaveValue('')
      await expect(page.getByText('Applied UTC window:', { exact: false })).toHaveCount(0)
      await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
      await expect.poll(() => JSON.stringify(state.exchanges.at(-1)?.params)).toBe(JSON.stringify({ limit: '100' }))
      await capture(page, `window-${role}-${mobile}-${scheme}-cleared`)
    })
  }

  for (const role of ['platform_admin', 'org_admin']) {
    await scenario({ name: `${role} rejects invalid windows with associated errors and keyboard focus`, mobile: true, scheme: 'dark', user: { role, is_platform_admin: role === 'platform_admin' } }, async (page, state) => {
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      const count = state.exchanges.length
      for (const label of ['Start time', 'End time']) for (const value of ['yesterday', '1700000000', '2026-10-21T00:00:00', '2026-02-30T00:00:00Z']) {
        await page.getByRole('button', { name: 'Clear filters', exact: true }).click()
        const field = page.getByLabel(label, { exact: true })
        await field.fill(value)
        await page.getByRole('button', { name: 'Apply filters', exact: true }).press('Enter')
        await expect(field).toBeFocused()
        await expect(field).toHaveAttribute('aria-invalid', 'true')
        const description = await field.getAttribute('aria-describedby')
        const error = await page.getByRole('alert').getAttribute('id')
        assert.ok(description.split(' ').includes(error))
        await expect(page.getByRole('alert')).toContainText('RFC 3339')
        assert.equal(state.exchanges.length, count)
      }
      await capture(page, `window-${role}-invalid-timestamp`)
      for (const value of ['2026-10-21T08:00:00+08:00', '2026-10-20T23:59:59Z']) {
        await page.getByLabel('Start time', { exact: true }).fill('2026-10-21T00:00:00Z')
        await page.getByLabel('End time', { exact: true }).fill(value)
        await page.getByRole('button', { name: 'Apply filters', exact: true }).press('Enter')
        const field = page.getByLabel('End time', { exact: true })
        await expect(field).toBeFocused()
        await expect(field).toHaveAttribute('aria-invalid', 'true')
        await expect(page.getByRole('alert')).toContainText('later than start')
        assert.equal(state.exchanges.length, count)
      }
      await capture(page, `window-${role}-invalid-order`)
      await page.getByLabel('End time', { exact: true }).fill('2026-10-22T00:00:00Z')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).press('Enter')
      await expect.poll(() => state.exchanges.length).toBe(count + 1)
      await expect(page.getByRole('alert')).toHaveCount(0)
    })

    for (const status of [200, 403]) await scenario({ name: `${role} ignores late older-window ${status}`, nextCursor: 'old-window-page-2', user: { role, is_platform_admin: role === 'platform_admin' } }, async (page, state) => {
      await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
      await page.getByLabel('Start time', { exact: true }).fill('2026-10-20T00:00:00Z')
      await page.getByLabel('End time', { exact: true }).fill('2026-10-22T00:00:00Z')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeEnabled()
      const count = state.exchanges.length
      state.gate = Promise.withResolvers(); state.status = status; state.result = [{ ...event, action: 'retired.window' }]
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect.poll(() => state.exchanges.length).toBe(count + 1)
      const pending = state.exchanges.at(-1)
      assert.equal(pending.params.start_at, '2026-10-20T00:00:00.000000Z')
      state.gate = null; state.status = 200; state.nextCursor = null; state.result = [{ ...event, action: 'current.window' }]
      await page.getByLabel('Start time', { exact: true }).fill('2026-10-21T00:00:00Z')
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'current.window', exact: true })).toBeVisible()
      assert.equal(state.exchanges.at(-1).params.start_at, '2026-10-21T00:00:00.000000Z')
      assert.equal(state.exchanges.at(-1).params.cursor, undefined)
      pending.gate.resolve(); await pending.delivered.promise; await flush(page)
      await expect(page.getByRole('heading', { name: 'retired.window', exact: true })).toHaveCount(0)
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      await capture(page, `window-${role}-late-${status}`)
    })
  }
}
