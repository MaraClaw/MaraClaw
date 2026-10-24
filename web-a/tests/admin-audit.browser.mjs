import assert from 'node:assert/strict'
import { expect as playwrightExpect } from '@playwright/test'

import { actorId, createHarness, event, flush, nullableEvent, signInReplacement, tenantId } from './admin-audit-browser-harness.mjs'
import { runPagingScenarios } from './admin-audit-paging.browser.mjs'

const expect = playwrightExpect.configure({ timeout: 15_000 })
const harness = await createHarness()
const { scenario, capture, base } = harness
let completed = false
try {
  for (const role of ['platform_admin', 'org_admin']) {
    for (const size of ['phone', 'desktop', 'wide']) for (const scheme of ['light', 'dark']) {
      const name = `${role}-${size}-${scheme}`
      await scenario({ name, user: { role, is_platform_admin: role === 'platform_admin' }, mobile: size === 'phone', width: size === 'wide' ? 1920 : 1440, scheme }, async (page, state) => {
        await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
        const nav = page.getByRole('link', { name: 'Activity', exact: true })
        await expect(nav).toHaveCount(1)
        await expect(nav).toHaveAttribute('aria-current', 'page')
        await expect(page.getByRole('link', { name: 'Companies', exact: true })).toHaveCount(role === 'platform_admin' ? 1 : 0)
        await expect(page.getByRole('link', { name: 'Search', exact: true })).toHaveCount(role === 'platform_admin' ? 1 : 0)
        await expect(page.getByLabel('Tenant ID', { exact: true })).toHaveCount(role === 'platform_admin' ? 1 : 0)
        await expect(page.getByLabel('Actor ID', { exact: true })).toHaveCount(role === 'platform_admin' ? 1 : 0)
        await expect(page.getByText('Time unavailable', { exact: true })).toBeVisible()
        await capture(page, `${name}-results`)
        const detail = page.locator('summary').first()
        await detail.focus()
        await detail.press('Enter')
        await expect(page.locator('details').first()).toHaveAttribute('open', '')
        await expect(page.locator('pre').first()).toContainText('is_active')
        await expect(page.locator('pre').nth(1)).toContainText('<script>literal, never executed</script>')
        await capture(page, `${name}-details-focus`)
        const lastDetail = page.locator('summary').last()
        await lastDetail.focus(); await lastDetail.press('Enter')
        await page.locator('main').evaluate((element) => { element.scrollTop = element.scrollHeight })
        await capture(page, `${name}-scrolled-end`)
        await page.getByLabel('Action', { exact: true }).fill('missing.action')
        state.result = []
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
        await expect(page.getByRole('heading', { name: '0 recent events' })).toBeVisible()
        await capture(page, `${name}-empty`)
        state.result = [event, nullableEvent]
        await page.getByRole('button', { name: 'Clear filters', exact: true }).click()
        await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
        await page.getByRole('link', { name: 'Account', exact: true }).click()
        await nav.click()
        await expect(page.getByRole('heading', { name: 'Recent activity', exact: true })).toBeVisible()
        assert.equal(state.exchanges[0].path, role === 'platform_admin' ? '/api/admin/audit-logs/page' : '/api/users/admin-audit-logs/page')
        assert.deepEqual(state.exchanges[0].params, { limit: '100' })
      })
    }
  }

  await scenario({ name: 'platform validates UUIDs and sends exact server filters', user: { role: 'platform_admin', is_platform_admin: true } }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    const initialCount = state.exchanges.length
    await page.getByLabel('Tenant ID', { exact: true }).fill('not-a-uuid')
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    await expect(page.getByLabel('Tenant ID', { exact: true })).toBeFocused()
    await expect(page.getByLabel('Tenant ID', { exact: true })).toHaveAttribute('aria-invalid', 'true')
    assert.equal(state.exchanges.length, initialCount)
    await capture(page, 'platform-invalid-filter')
    await page.getByLabel('Tenant ID', { exact: true }).fill(` ${tenantId} `)
    await page.getByLabel('Actor ID', { exact: true }).fill(actorId)
    await page.getByLabel('Action', { exact: true }).fill(' user.activate&limit=501 ')
    await page.getByRole('button', { name: 'Apply filters', exact: true }).press('Enter')
    await expect.poll(() => state.exchanges.length).toBe(initialCount + 1)
    assert.deepEqual(state.exchanges.at(-1).params, { limit: '100', action: 'user.activate&limit=501', tenant_id: tenantId, actor_id: actorId })
  })

  for (const status of [403, 503, 'malformed', 'offline']) await scenario({ name: `${status} failure recovers in current filter` }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    if (status === 'offline') await page.context().setOffline(true)
    else if (status === 'malformed') state.result = [{ ...event, details: null }]
    else state.status = status
    await page.getByLabel('Action', { exact: true }).fill('failed.filter')
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    if (status === 'offline') {
      await expect(page.getByRole('status').filter({ hasText: 'Waiting for a connection.' })).toBeVisible()
      await capture(page, 'offline-waiting')
      state.status = 503
      await page.context().setOffline(false)
    }
    await expect(page.getByRole('alert')).toContainText('Could not load recent activity.')
    await expect(page.getByRole('heading', { name: event.action, exact: true })).toHaveCount(0)
    await capture(page, `failure-${status}`)
    state.status = 200; state.result = [event]
    await page.getByRole('button', { name: 'Try again', exact: true }).click()
    await expect(page.getByRole('heading', { name: '1 recent event', exact: true })).toBeVisible()
    assert.equal(state.exchanges.at(-1).params.action, 'failed.filter')
  })

  for (const lateStatus of [200, 403]) await scenario({ name: `late filter ${lateStatus} cannot replace new filter results` }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    const initialCount = state.exchanges.length
    state.gate = Promise.withResolvers(); state.status = lateStatus
    state.result = [{ ...event, action: 'retired.filter.result' }]
    await page.getByLabel('Action', { exact: true }).fill('slow.filter')
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    await expect.poll(() => state.exchanges.length).toBe(initialCount + 1)
    const pending = state.exchanges.at(-1)
    await expect(page.getByText('Loading recent activity...', { exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: event.action, exact: true })).toHaveCount(0)
    await capture(page, `filter-${lateStatus}-loading`)
    state.gate = null; state.status = 200; state.result = [{ ...event, action: 'current.filter.result' }]
    await page.getByLabel('Action', { exact: true }).fill('new.filter')
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'current.filter.result', exact: true })).toBeVisible()
    pending.gate.resolve(); await pending.delivered.promise; await flush(page)
    await expect(page.getByRole('heading', { name: 'retired.filter.result', exact: true })).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await capture(page, `filter-${lateStatus}-current`)
  })

  for (const sameToken of [true, false]) for (const status of [200, 401, 403]) await scenario({ name: `late older-page ${status} after ${sameToken ? 'same' : 'different'} token session replacement`, nextCursor: 'retired-owner-page-2', user: { role: 'platform_admin', is_platform_admin: true } }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    await page.getByLabel('Action', { exact: true }).fill('retired.owner.filter')
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    await expect.poll(() => state.exchanges.at(-1)?.params.action).toBe('retired.owner.filter')
    await expect(page.getByRole('button', { name: 'Older', exact: true })).toBeEnabled()
    const initialCount = state.exchanges.length
    state.gate = Promise.withResolvers(); state.status = status
    state.result = [{ ...event, action: 'retired.owner.result' }]
    await page.getByRole('button', { name: 'Older', exact: true }).click()
    await expect.poll(() => state.exchanges.length).toBe(initialCount + 1)
    const pending = state.exchanges.at(-1)
    assert.equal(pending.params.cursor, 'retired-owner-page-2')
    state.user = { ...state.user, id: tenantId, tenant_id: actorId, role: 'org_admin', is_platform_admin: false }
    if (!sameToken) state.token = 'synthetic-replacement-token'
    state.gate = null; state.status = 200; state.nextCursor = null
    state.result = [{ ...event, action: 'replacement.owner.result' }]
    await signInReplacement(page)
    await expect(page.getByRole('heading', { name: 'replacement.owner.result', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
    assert.equal(state.exchanges.at(-1).path, '/api/users/admin-audit-logs/page')
    assert.deepEqual(state.exchanges.at(-1).params, { limit: '100' })
    pending.gate.resolve(); await pending.delivered.promise; await flush(page)
    await expect(page.getByRole('heading', { name: 'retired.owner.result', exact: true })).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page).toHaveURL(`${base}activity`)
    await capture(page, `replacement-${sameToken}-${status}`)
  })

  await runPagingScenarios(harness)

  for (const mobile of [false, true]) await scenario({ name: `${mobile ? 'phone' : 'desktop'} password requirement locks direct route and navigation`, mobile, user: { must_change_password: true } }, async (page, state) => {
    await expect(page).toHaveURL(`${base}account`)
    await expect(page.getByRole('link', { name: 'Activity', exact: true })).toHaveCount(0)
    await expect(page.locator('[aria-disabled="true"]:visible').filter({ hasText: /^Activity$/ })).toHaveCount(1)
    assert.equal(state.exchanges.length, 0)
    await capture(page, `password-gate-${mobile}`)
  })

  await scenario({ name: 'member cannot open activity', user: { role: 'member' } }, async (page, state) => {
    await expect(page).toHaveURL(`${base}login`)
    await expect(page.getByRole('heading', { name: 'Recent activity', exact: true })).toHaveCount(0)
    assert.equal(state.exchanges.length, 0)
  })

  await scenario({ name: 'unbroken details reflow at 320px and 200 percent text', mobile: true, scheme: 'dark' }, async (page, state) => {
    await expect(page.getByRole('heading', { name: '2 recent events' })).toBeVisible()
    state.result = [{ ...event, action: 'VeryLongActionWithoutBreaks'.repeat(10), details: { payload: 'UnbrokenContent'.repeat(100) } }]
    await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
    await expect(page.getByRole('heading', { name: '1 recent event', exact: true })).toBeVisible()
    await page.locator('summary').press('Enter')
    await page.setViewportSize({ width: 320, height: 700 })
    await page.addStyleTag({ content: 'html { font-size: 200% }' })
    await page.locator('main').evaluate((element) => { element.scrollTop = element.scrollHeight })
    await capture(page, 'long-content-320px-200percent')
  })
  completed = true
} finally {
  await harness.close(completed)
}
