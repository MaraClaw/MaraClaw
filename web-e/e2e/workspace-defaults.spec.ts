import { expect, test } from '@playwright/test'

import { originalAgent, prepareWorkspace } from './session-fencing-fixture'

for (const selection of ['default', 'explicit'] as const) {
  test(`OKR ${selection} period matches the requested objectives`, async ({ page }) => {
    await prepareWorkspace(page)
    const previous = { start: '2026-07-01', end: '2026-09-30', label: 'Previous quarter', is_current: false }
    const current = { start: '2026-10-01', end: '2026-12-31', label: 'Current quarter', is_current: true }
    await page.route('**/api/okr/**', async (route) => {
      const url = new URL(route.request().url())
      switch (url.pathname) {
        case '/api/okr/periods':
          await route.fulfill({ json: [previous, current] })
          break
        case '/api/okr/settings':
          await route.fulfill({ json: { enabled: false, period_frequency: 'quarterly' } })
          break
        case '/api/okr/objectives': {
          const period = url.searchParams.get('period_start') === previous.start ? previous : current
          expect(url.searchParams.get('period_end')).toBe(period.end)
          await route.fulfill({ json: [{ id: period.start, title: `${period.label} objective`, owner_type: 'company', status: 'active', key_results: [] }] })
          break
        }
        default:
          await route.fulfill({ json: [] })
      }
    })

    await page.goto('/app/okr')
    await expect(page.getByLabel('Period')).toHaveValue(current.start)
    if (selection === 'explicit') await page.getByLabel('Period').selectOption(previous.start)

    const expected = selection === 'explicit' ? previous : current
    await expect(page.getByLabel('Period')).toHaveValue(expected.start)
    await expect(page.getByText(`${expected.label} objective`, { exact: true })).toBeVisible()
  })
}

for (const selection of ['default', 'query', 'explicit'] as const) {
  test(`Take Control locks the ${selection} session displayed in its selector`, async ({ page }) => {
    await prepareWorkspace(page)
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
    await page.route(`**/api/agents/${originalAgent.id}/sessions*`, (route) => route.fulfill({ json: [
      { id: 'first', title: 'First thread' }, { id: 'second', title: 'Second thread' },
    ] }))
    const bodies: unknown[] = []
    await page.route(`**/api/agents/${originalAgent.id}/control/*`, async (route) => {
      if (new URL(route.request().url()).pathname.endsWith('/lock')) bodies.push(route.request().postDataJSON())
      await route.fulfill({ json: { status: 'ok' } })
    })
    await page.goto(`/app/agents/${originalAgent.id}/control${selection === 'query' ? '?session=second&env=computer' : ''}`)
    await expect(page.getByLabel('Chat session')).toHaveValue(selection === 'query' ? 'second' : 'first')
    if (selection === 'explicit') await page.getByLabel('Chat session').selectOption('second')

    await page.getByRole('button', { name: 'Take control', exact: true }).click()

    await expect(page.getByLabel('Chat session')).toBeDisabled()
    expect(bodies).toEqual([{ session_id: selection === 'default' ? 'first' : 'second', env_type: selection === 'query' ? 'computer' : 'browser' }])
    await expect(page).toHaveURL(new RegExp(`session=${selection === 'default' ? 'first' : 'second'}`))
  })
}
