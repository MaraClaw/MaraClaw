// Standalone synthetic QA: node tests/search-analytics.browser.mjs
// SEARCH_ANALYTICS_BASELINE=1 captures the unchanged legacy dashboard before implementation.
import assert from 'node:assert/strict'
import { createHarness, expect, companyId, otherCompanyId, flush } from './search-analytics-browser-harness.mjs'

const baseline = process.env.SEARCH_ANALYTICS_BASELINE === '1'
const harness = await createHarness()
let completed = false
try {
  for (const width of [1440, 390, 1920]) for (const scheme of ['light', 'dark']) {
    const name = `${width === 390 ? 'phone' : width === 1440 ? 'desktop' : 'wide'}-${scheme}-${baseline ? 'before' : 'after'}`
    await harness.scenario({ name, mobile: width === 390, width, scheme }, async (page, state) => {
      await expect(page.getByText('system query', { exact: true })).toBeVisible()
      await expect(page.getByRole('heading', { name: '100', exact: true })).toBeVisible()
      const renderedAt = performance.now()
      const metrics = state.exchanges.filter(({ path }) => /search-analytics\/(dashboard|summary|timeseries|orgs|trending)$/.test(path))
      assert.equal(metrics.length, baseline ? 5 : 1)
      harness.receipt.timings.push({ scenario: name, dashboardRequests: metrics.length, requestToRenderedMs: renderedAt - metrics[0].at })
      await harness.capture(page, name)
      const independent = state.exchanges.filter(({ path }) => /companies\/options|export-status/.test(path)).length
      await page.getByLabel('Analytics scope').selectOption(companyId)
      await expect(page.getByText('company query', { exact: true })).toBeVisible()
      await expect(page.getByText('25 of 100', { exact: false })).toBeVisible()
      assert.equal(state.exchanges.filter(({ path }) => /companies\/options|export-status/.test(path)).length, independent)
      if (!baseline) {
        assert.equal(state.exchanges.filter(({ path }) => path.endsWith('/dashboard')).length, 2)
        const [system, selected] = state.exchanges.filter(({ path }) => path.endsWith('/dashboard'))
        assert.equal(selected.params.tenant_id, companyId)
        assert.equal(selected.params.start, system.params.start)
        assert.equal(selected.params.end, system.params.end)
        assert.equal(new URL(page.url()).searchParams.get('company'), companyId)
      }
      await harness.capture(page, `${name}-selected`)
      await page.getByLabel('Analytics scope').focus()
      await harness.capture(page, `${name}-selected-focus`)
      await page.locator('#search-engine-panel-analytics').evaluate((element) => { element.parentElement.parentElement.scrollTop = element.parentElement.parentElement.scrollHeight })
      await expect(page.getByText('3 pending.', { exact: false })).toBeVisible()
      await harness.capture(page, `${name}-scrolled-end`)
    })
  }
  if (!baseline) {
    for (const lateStatus of [200, 503]) await harness.scenario({ name: `rapid range and tenant changes discard late ${lateStatus}`, path: 'search-engine?tab=analytics&keep=value' }, async (page, state) => {
      await expect(page.getByText('system query', { exact: true })).toBeVisible()
      state.gate = Promise.withResolvers(); state.status = lateStatus; state.count = 400
      await page.getByRole('combobox', { name: /^Range/ }).selectOption('30')
      await expect.poll(() => state.exchanges.filter(({ path }) => path.endsWith('/dashboard')).length).toBe(2)
      const slow = state.exchanges.filter(({ path }) => path.endsWith('/dashboard')).at(-1)
      await expect(page.getByText('Loading chart…', { exact: true })).toBeVisible()
      await expect(page.getByRole('heading', { name: '100', exact: true })).toHaveCount(0)
      state.gate = null; state.status = 200; state.count = 800
      await page.getByLabel('Analytics scope').selectOption(companyId)
      await expect(page.getByRole('heading', { name: '200', exact: true })).toBeVisible()
      await page.getByRole('combobox', { name: /^Range/ }).selectOption('90')
      await expect.poll(() => state.exchanges.filter(({ path }) => path.endsWith('/dashboard')).length).toBe(4)
      await expect(page.getByRole('heading', { name: '200', exact: true })).toBeVisible()
      slow.gate.resolve(); await slow.delivered.promise; await flush(page)
      await expect(page.getByRole('heading', { name: '200', exact: true })).toBeVisible()
      await expect(page.getByText('Synthetic analytics unavailable', { exact: true })).toHaveCount(0)
      const metrics = state.exchanges.filter(({ path }) => path.endsWith('/dashboard'))
      assert.equal(metrics.length, 4)
      const last = metrics.at(-1).params
      assert.equal(last.tenant_id, companyId)
      assert.equal(Date.parse(last.end) - Date.parse(last.start), 90 * 86_400_000)
      assert.notEqual(last.start, metrics[1].params.start)
      assert.equal(new URL(page.url()).searchParams.get('keep'), 'value')
      await harness.capture(page, `rapid-${lateStatus}`)
    })

    for (const sameToken of [false, true]) for (const lateStatus of [200, 401, 503]) {
      await harness.scenario({ name: `replacement ${sameToken ? 'same' : 'new'} token discards late ${lateStatus}` }, async (page, state) => {
        await expect(page.getByText('system query', { exact: true })).toBeVisible()
        state.gate = Promise.withResolvers(); state.status = lateStatus; state.count = 400
        await page.getByRole('combobox', { name: /^Range/ }).selectOption('30')
        await expect.poll(() => state.exchanges.filter(({ path }) => path.endsWith('/dashboard')).length).toBe(2)
        const slow = state.exchanges.filter(({ path }) => path.endsWith('/dashboard')).at(-1)
        state.gate = null; state.status = 200; state.count = 800
        state.user = { ...state.user, id: companyId, identity_id: companyId, display_name: 'Replacement operator' }
        if (!sameToken) state.token = 'replacement-analytics-token'
        await page.getByRole('button', { name: 'Sign out', exact: true }).click()
        await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
        await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
        await page.getByRole('button', { name: 'Sign in', exact: true }).click()
        await page.getByRole('tab', { name: 'Analytics', exact: true }).click()
        await expect(page.getByRole('heading', { name: '800', exact: true })).toBeVisible()
        slow.gate.resolve(); await slow.delivered.promise; await flush(page)
        await expect(page.getByRole('heading', { name: '800', exact: true })).toBeVisible()
        await expect(page.getByText('Synthetic analytics unavailable', { exact: true })).toHaveCount(0)
        await expect(page.getByRole('combobox', { name: /^Range/ })).toHaveValue('7')
        await expect(page).toHaveURL(`${harness.base}search-engine?tab=analytics`)
      })
    }

    await harness.scenario({ name: 'empty dashboard keeps zero totals and padded chart' }, async (page, state) => {
      await expect(page.getByText('system query', { exact: true })).toBeVisible()
      state.count = 0
      await page.getByRole('combobox', { name: /^Range/ }).selectOption('30')
      await expect(page.getByText('No trending queries in this range.', { exact: true })).toBeVisible()
      await expect(page.getByText('No per-company volume yet.', { exact: true })).toBeVisible()
      await expect(page.getByText('No kind breakdown yet.', { exact: true })).toBeVisible()
      await expect(page.getByRole('img', { name: 'Daily search volume' })).toBeVisible()
      await expect(page.getByRole('heading', { name: '0', exact: true })).toHaveCount(2)
      await harness.capture(page, 'empty')
    })

    for (const failure of [503, 'malformed']) await harness.scenario({ name: `dashboard ${failure} remains an error with independent export and filters` }, async (page, state) => {
      await expect(page.getByText('system query', { exact: true })).toBeVisible()
      if (failure === 'malformed') state.overrides = { selected_summary: {} }
      else state.status = failure
      await page.getByRole('combobox', { name: /^Range/ }).selectOption('30')
      await expect(page.getByText(failure === 'malformed' ? 'Failed to load search analytics' : 'Synthetic analytics unavailable', { exact: true })).toBeVisible()
      await expect(page.getByRole('heading', { name: '100', exact: true })).toHaveCount(0)
      await expect(page.getByRole('heading', { name: '0', exact: true })).toHaveCount(0)
      assert.equal(state.exchanges.filter(({ path }) => /search-analytics\/(summary|timeseries|orgs|trending)$/.test(path)).length, 0)
      await harness.capture(page, `failure-${failure}`)
      state.status = 200; state.overrides = {}
      await page.getByLabel('Analytics scope').selectOption(otherCompanyId)
      await expect(page.getByText('company query', { exact: true })).toBeVisible()
    })

    await harness.scenario({ name: 'Overview retains legacy reads and tab navigation preserves URL rules', path: '' }, async (page, state) => {
      await expect(page.getByText('Busiest: Global leader.', { exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges.filter(({ path }) => path.includes('/search-analytics/')).map(({ path }) => path.split('/').at(-1)).sort(), ['orgs', 'summary'])
      await page.getByRole('link', { name: 'Open dashboard', exact: true }).click()
      await expect(page.getByText('system query', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Global leader', exact: false }).click()
      await expect(page.getByLabel('Analytics scope')).toHaveValue(otherCompanyId)
      await page.getByRole('tab', { name: 'Keys', exact: true }).click()
      await expect(page).toHaveURL(`${harness.base}search-engine`)
      await page.getByRole('tab', { name: 'Analytics', exact: true }).focus()
      await page.keyboard.press('Enter')
      await expect(page.getByLabel('Analytics scope')).toHaveValue('')
    })
  }
  completed = true
} finally { await harness.close(completed) }
