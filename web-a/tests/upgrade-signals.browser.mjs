// From web-a: UPGRADE_SIGNALS_ARTIFACTS=/persistent/evidence node tests/upgrade-signals.browser.mjs
// --baseline captures the unchanged summary before implementation; no live APIs are used.
// Optional UPGRADE_SIGNALS_MATCH selects scenario names by regular expression.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createHarness, expect, originalSearch } from './upgrade-signals-harness.browser.mjs'
import { agentId, csvBody, otherTenantId, tenantId } from './resource-pressure-fixtures.ts'

const harness = await createHarness({ match: process.env.UPGRADE_SIGNALS_MATCH })
const detailPath = `upgrade-signals/${tenantId}${originalSearch}`
const expectedFilters = { since: '2026-11-01T00:00:00.000Z', until: '2026-11-16T00:00:00.000Z', tier: 'pro', resource: 'memory' }
const pressureRequests = (state) => state.requests.filter((request) => request.path.includes('/resource-pressure/'))
let completed = false
try {
  for (const mobile of [false, true]) for (const scheme of ['light', 'dark']) {
    const name = `${mobile ? 'mobile' : 'desktop'}-${scheme}`
    await harness.scenario({ name, mobile, scheme }, async (page, state) => {
      await expect(page.getByRole('heading', { name: 'Summary by company' })).toBeVisible()
      await expect(page.getByText('Companies: 2. With a suggested upgrade: 1.', { exact: true })).toBeVisible()
      await page.getByRole('heading', { name: 'Summary by company' }).scrollIntoViewIfNeeded()
      await harness.capture(page, `${name}-${process.argv.includes('--baseline') ? 'before' : 'after'}`)
      if (process.argv.includes('--baseline')) return
      const link = page.getByRole('link', { name: 'Open Acme Corp', exact: true })
      await expect(page.getByRole('link', { name: /^Open / })).toHaveCount(2)
      await expect(page.getByRole('heading', { name: /^Episodes for / })).toHaveCount(0)
      assert.equal(state.requests.some((request) => request.path.endsWith('/episodes')), false)
      const card = link.locator('..')
      assert.deepEqual(await card.locator('dt').allTextContents(), ['Current tier', 'Suggested tier', 'Memory episodes', 'CPU episodes', 'Workspace disk episodes', 'Managed storage episodes', 'OOM kills', 'Time under pressure', 'Peak usage', 'Agents affected', 'Last seen'])
      assert.deepEqual((await card.locator('dd').allTextContents()).slice(0, 10), ['PRO', 'MAX', '4', '1', '0', '2', '1', '2h 5m', '97.3%', '3'])
      await expect(card.locator('time')).toHaveAttribute('datetime', '2026-11-12T08:30:00Z')
      await link.scrollIntoViewIfNeeded()
      await harness.capture(page, `${name}-card`)
      await link.click()
      await expect(page).toHaveURL(`${harness.base}${detailPath}`)
      await expect(page.getByRole('heading', { name: 'Upgrade signals for Acme Corp', exact: true })).toBeVisible()
      await expect(page.getByRole('heading', { name: 'Episodes for Acme Corp', exact: true })).toBeVisible()
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
      await expect(page.getByText('Globex', { exact: true })).toHaveCount(0)
      await harness.capture(page, `${name}-detail`)
      await page.getByRole('heading', { name: 'Episodes for Acme Corp' }).scrollIntoViewIfNeeded()
      await harness.capture(page, `${name}-episodes`)
      await page.getByRole('link', { name: 'Back to summary', exact: true }).click()
      await expect(page).toHaveURL(`${harness.base}upgrade-signals${originalSearch}`)
      await expect(page.getByLabel('From', { exact: true })).toHaveValue('2026-11-01')
      await expect(page.getByLabel('To', { exact: true })).toHaveValue('2026-11-15')
      await expect(page.getByLabel('Tier when recorded')).toHaveValue('pro')
      await expect(page.getByLabel('Resource', { exact: true })).toHaveValue('memory')
      await page.getByRole('button', { name: 'Download CSV', exact: true }).focus()
      await page.keyboard.press('Tab')
      await expect(link).toBeFocused()
      await harness.capture(page, `${name}-card-keyboard-focus`)
      await page.keyboard.press('Enter')
      await expect(page.getByRole('heading', { name: 'Upgrade signals for Acme Corp', exact: true })).toBeVisible()
    })
  }

  if (!process.argv.includes('--baseline')) {
    await harness.scenario({ name: 'direct detail reload and back preserve original company filter', path: `${detailPath}&company=${otherTenantId}` }, async (page, state) => {
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
      await page.reload()
      await expect(page.getByRole('heading', { name: 'Upgrade signals for Acme Corp' })).toBeVisible()
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
      await expect(page.getByText('Globex', { exact: true })).toHaveCount(0)
      for (const request of pressureRequests(state)) assert.equal(request.params.tenant_id, tenantId)
      assert.deepEqual(pressureRequests(state).find((request) => request.path.endsWith('/summary')).params, { ...expectedFilters, tenant_id: tenantId })
      await page.getByRole('link', { name: 'Back to summary', exact: true }).click()
      await expect(page).toHaveURL(`${harness.base}upgrade-signals${originalSearch}&company=${otherTenantId}`)
      await expect(page.getByLabel('Company', { exact: true })).toHaveValue(otherTenantId)
      await expect(page.getByRole('link', { name: 'Open Globex', exact: true })).toBeVisible()
      await expect(page.getByRole('link', { name: 'Open Acme Corp', exact: true })).toHaveCount(0)
    })

    await harness.scenario({ name: 'episode filters paging and CSV stay company scoped', path: detailPath }, async (page, state) => {
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Older', exact: true }).click()
      await expect(page.getByText('Older assistant', { exact: true })).toBeVisible()
      await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Back', exact: true }).click()
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
      await page.getByLabel('Agent ID', { exact: true }).fill('invalid')
      const beforeInvalid = pressureRequests(state).length
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByLabel('Agent ID', { exact: true })).toHaveAttribute('aria-invalid', 'true')
      assert.equal(pressureRequests(state).length, beforeInvalid)
      await page.getByLabel('Agent ID', { exact: true }).fill(agentId)
      await page.getByLabel('Kind', { exact: true }).selectOption('threshold')
      await page.getByLabel('Open episodes only', { exact: true }).check()
      await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
      await expect(page.getByText('Filtered assistant', { exact: true })).toBeVisible()
      await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
      const downloadReady = page.waitForEvent('download')
      await page.getByRole('button', { name: 'Download CSV for Acme Corp', exact: true }).click()
      const download = await downloadReady
      assert.equal(download.suggestedFilename(), 'resource-pressure-episodes.csv')
      assert.equal(await readFile(await download.path(), 'utf8'), csvBody)
      const csv = state.requests.find((request) => request.path.endsWith('/export.csv'))
      assert.deepEqual(csv.params, { ...expectedFilters, tenant_id: tenantId, agent_id: agentId, kind: 'threshold', open_only: 'true' })
      await page.getByRole('button', { name: 'Clear filters', exact: true }).click()
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
      await expect(page.getByLabel('Agent ID', { exact: true })).toHaveValue('')
      await expect(page.getByLabel('Kind', { exact: true })).toHaveValue('')
      await expect(page.getByLabel('Open episodes only', { exact: true })).not.toBeChecked()
    })

    await harness.scenario({ name: 'summary CSV retains current filters' }, async (page, state) => {
      await expect(page.getByRole('link', { name: 'Open Acme Corp', exact: true })).toBeVisible()
      const downloadReady = page.waitForEvent('download')
      await page.getByRole('button', { name: 'Download CSV', exact: true }).click()
      const download = await downloadReady
      assert.equal(await readFile(await download.path(), 'utf8'), csvBody)
      assert.deepEqual(state.requests.find((request) => request.path.endsWith('/export.csv')).params, expectedFilters)
    })

    for (const mobile of [false, true]) for (const scheme of ['light', 'dark']) {
      const name = `${mobile ? 'mobile' : 'desktop'}-${scheme}`
      await harness.scenario({ name: `${name} empty summary`, mobile, scheme, rows: [] }, async (page) => {
        await expect(page.getByText('No resource pressure recorded in this period', { exact: true })).toBeVisible()
        await expect(page.getByRole('link', { name: /^Open / })).toHaveCount(0)
        await page.getByRole('heading', { name: 'Summary by company' }).scrollIntoViewIfNeeded()
        await harness.capture(page, `${name}-empty-summary`)
      })
      await harness.scenario({ name: `${name} failed summary retry`, mobile, scheme, summaryStatus: 503 }, async (page, state) => {
        await expect(page.getByRole('alert').filter({ hasText: 'Synthetic summary unavailable' })).toBeVisible()
        await page.getByRole('button', { name: 'Try again', exact: true }).scrollIntoViewIfNeeded()
        await harness.capture(page, `${name}-failed-summary`)
        state.summaryStatus = 200
        await page.getByRole('button', { name: 'Try again', exact: true }).click()
        await expect(page.getByRole('link', { name: 'Open Acme Corp', exact: true })).toBeVisible()
      })
      await harness.scenario({ name: `${name} empty detail`, mobile, scheme, path: detailPath, rows: [], emptyEpisodes: true }, async (page) => {
        await expect(page.getByText('No resource pressure recorded in this period', { exact: true })).toHaveCount(2)
        await expect(page.getByRole('link', { name: 'Back to summary', exact: true })).toBeVisible()
        await harness.capture(page, `${name}-empty-detail`)
      })
    }

    for (const failure of ['summary', 'episodes']) await harness.scenario({ name: `detail ${failure} failure recovers`, path: detailPath, summaryStatus: failure === 'summary' ? 503 : 200, episodeStatus: failure === 'episodes' ? 503 : 200 }, async (page, state) => {
      await expect(page.getByRole('alert').filter({ hasText: `Synthetic ${failure} unavailable` })).toBeVisible()
      state.summaryStatus = 200; state.episodeStatus = 200
      await page.getByRole('button', { name: 'Try again', exact: true }).click()
      await expect(page.getByText('Sales assistant', { exact: true })).toBeVisible()
    })

    for (const path of [`upgrade-signals${originalSearch}`, detailPath]) await harness.scenario({ name: `org admin denied ${path}`, path, orgAdmin: true }, async (page, state) => {
      await expect(page).toHaveURL(harness.base)
      await expect(page.getByRole('link', { name: 'Upgrade signals', exact: true })).toHaveCount(0)
      assert.deepEqual(pressureRequests(state), [])
    })

    await harness.scenario({ name: 'invalid company deep link sends no pressure reads', path: 'upgrade-signals/not-a-uuid' }, async (page, state) => {
      await expect(page.getByRole('alert')).toHaveText('That company was not found.')
      assert.deepEqual(pressureRequests(state), [])
    })
  }
  completed = true
} finally { await harness.close(completed) }
