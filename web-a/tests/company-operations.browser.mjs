// COMPANY_OPERATIONS_ARTIFACTS=/persistent/evidence node tests/company-operations.browser.mjs
import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { company, companyHarness } from './company-operations-harness.mjs'
import { runtimePage, tierPreview } from './company-operations-fixtures.ts'

const harness = await companyHarness()
const json = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
function runtimeHandler(mode) {
  return async (route, url) => {
    if (!url.pathname.endsWith('/runtime')) return false
    const next = url.searchParams.has('cursor')
    const value = mode === 'empty' ? { ...runtimePage, items: [] }
      : mode === 'unavailable' ? { ...runtimePage, observation_available: false, items: runtimePage.items.map(agent => ({ ...agent, observation: { state: 'unavailable' } })) }
      : { ...runtimePage, items: runtimePage.items.map(agent => ({ ...agent, name: next ? 'Second Agent' : 'Synthetic Agent',
        workspace: { ...agent.workspace, used_bytes: next ? 1 : 0 },
        runtime_loss: { reason: 'container_stopped', detected_at: '2026-11-15T03:00:00Z' },
        start: { operation_id: company.id, state: 'pending', attempts: 2, last_attempt_at: '2026-11-15T03:00:00Z', next_retry_at: '2026-11-15T04:01:00Z', last_failure_code: 'capacity_exceeded' },
      })), next_cursor: next ? null : 'opaque/next +?=' }
    await json(route, value)
    return true
  }
}

try {
  for (const width of [1440, 390, 1920]) for (const theme of ['light', 'dark']) {
    const mobile = width === 390
    const suffix = `${width}-${theme}`
    for (const mode of ['populated', 'empty', 'unavailable']) {
      await harness.scenario(`${mode}-${suffix}`, { mobile, theme, width }, async (page, state, base) => {
        state.handle = runtimeHandler(mode)
        await page.goto(`${base}companies/${company.id}`)
        await expect(page.getByText('Agents on first page', { exact: true })).toBeVisible()
        await harness.capture(page, `detail-${mode}-${suffix}-after`)
        await page.getByRole('link', { name: 'Runtime inventory', exact: true }).click()
        await expect(page).toHaveURL(`${base}companies/${company.id}/runtimes`)
        await expect(page.getByRole('heading', { name: 'Runtime inventory' })).toBeVisible()
        if (mode === 'empty') await expect(page.getByText('No agent runtimes on this page. Refresh to check for new agents.')).toBeVisible()
        else if (mode === 'unavailable') {
          await expect(page.getByText('Docker observations are unavailable.', { exact: false })).toBeVisible()
          await expect(page.getByText('Observation unavailable', { exact: true })).toBeVisible()
          await expect(page.getByText('Unknown', { exact: true }).first()).toBeVisible()
        } else {
          await expect(page.getByRole('heading', { name: 'Synthetic Agent' })).toBeVisible()
          await expect(page.getByText('Restarts since last start')).toBeVisible()
          await expect(page.getByText('capacity_exceeded')).toBeVisible()
          await expect(page.getByText('0 B / 1 GiB', { exact: true })).toBeVisible()
        }
        await harness.capture(page, `runtime-${mode}-${suffix}-after`)
        if (mode === 'populated') {
          await page.getByRole('button', { name: 'Next page', exact: true }).click()
          await expect(page.getByRole('heading', { name: 'Second Agent' })).toBeVisible()
          await expect(page.getByText('1 B / 1 GiB', { exact: true })).toBeVisible()
          await expect(page.getByRole('button', { name: 'Next page', exact: true })).toBeDisabled()
          await harness.capture(page, `runtime-paging-${suffix}-after`)
          await page.getByRole('button', { name: 'Previous page', exact: true }).click()
          await expect(page.getByRole('heading', { name: 'Synthetic Agent' })).toBeVisible()
        }
      })
    }
    await harness.scenario(`tier-conflict-${suffix}`, { mobile, theme, width }, async (page, state, base) => {
      let conflicted = false
      let applied = false
      state.handle = async (route, url) => {
        if (await runtimeHandler('empty')(route, url)) return true
        if (url.pathname.endsWith('/tier/preview')) {
          assert.deepEqual(route.request().postDataJSON(), { target_tier: 'pro' })
          await json(route, { ...tierPreview, current_tier: state.company.tier })
          return true
        }
        if (url.pathname.endsWith('/tier')) {
          const body = route.request().postDataJSON()
          if (!conflicted) {
            assert.deepEqual(body, { target_tier: 'pro', expected_tier: 'max' })
            conflicted = true
            state.company.tier = 'ultra'
            await json(route, { detail: { code: 'company_tier_changed', message: 'Company tier changed' } }, 409)
          } else {
            assert.deepEqual(body, { target_tier: 'pro', expected_tier: 'ultra' })
            applied = true
            state.company.tier = 'pro'
            await json(route, { tenant_id: company.id, previous_tier: 'ultra', tier: 'pro', changed: true, closed_pressure_episodes: 2 })
          }
          return true
        }
        return false
      }
      await page.goto(`${base}companies/${company.id}`)
      await page.getByLabel('Target tier', { exact: true }).selectOption('pro')
      await page.getByRole('button', { name: 'Preview tier change', exact: true }).click()
      await expect(page.getByText('Tier change: MAX to PRO')).toBeVisible()
      await expect(page.getByRole('button', { name: 'Confirm tier change', exact: true })).toBeDisabled()
      await harness.capture(page, `detail-preview-${suffix}-after`)
      await page.getByLabel('I reviewed the tier impact for this company.').check()
      await page.getByRole('button', { name: 'Confirm tier change', exact: true }).click()
      await expect(page.getByLabel('Target tier', { exact: true })).toHaveValue('pro')
      await expect(page.getByText('Tier change: ULTRA to PRO')).toBeVisible()
      await expect(page.getByRole('button', { name: 'Confirm tier change', exact: true })).toBeDisabled()
      await expect(page.getByLabel('I reviewed the tier impact for this company.')).not.toBeChecked()
      await expect(page.getByText('The company tier changed. Review the refreshed preview before confirming again.')).toBeVisible()
      await harness.capture(page, `detail-conflict-${suffix}-after`)
      await page.getByLabel('I reviewed the tier impact for this company.').check()
      await page.getByRole('button', { name: 'Confirm tier change', exact: true }).click()
      await expect(page.getByLabel('I reviewed the tier impact for this company.')).toHaveCount(0)
      await expect(page.getByLabel('Target tier', { exact: true })).toHaveValue('pro')
      assert.equal(applied, true)
    })
    await harness.scenario(`desired-enable-${suffix}`, { mobile, theme, width }, async (page, state, base) => {
      state.handle = async (route, url) => {
        if (!url.pathname.endsWith('/toggle')) return false
        assert.deepEqual(route.request().postDataJSON(), { is_active: true })
        state.company.is_active = true
        await json(route, { ok: true, is_active: true, restored_members: 3, inactive_members: 4 })
        return true
      }
      await page.goto(`${base}companies`)
      await page.getByRole('button', { name: 'Enable Synthetic Company' }).click()
      await expect(page.getByText('Company enabled. 3 members restored; 4 remain inactive.')).toBeVisible()
      await expect(page.getByRole('button', { name: 'Disable Synthetic Company' })).toBeVisible()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
      const path = `${harness.output}/desired-enable-${suffix}-after.png`
      await page.screenshot({ path, fullPage: true, animations: 'disabled' })
      harness.receipt.screenshots.push(path)
    })
    await harness.scenario(`system-tier-locked-${suffix}`, { mobile, theme, width }, async (page, state, base) => {
      state.company.is_system = true
      state.company.can_disable = false
      state.handle = runtimeHandler('empty')
      await page.goto(`${base}companies/${company.id}`)
      await expect(page.getByText('System company tiers are managed by the platform and cannot be changed here.')).toBeVisible()
      await expect(page.getByLabel('Target tier', { exact: true })).toHaveCount(0)
      await harness.capture(page, `detail-system-${suffix}-after`)
      await page.goto(`${base}companies`)
      await expect(page.getByRole('button', { name: 'Enable Synthetic Company' })).toBeDisabled()
    })
  }
  assert.equal(harness.receipt.requests.some(request => request.path.startsWith('/api/agents/')), false)
} finally { await harness.close() }
