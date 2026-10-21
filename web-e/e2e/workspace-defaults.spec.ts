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

for (const selection of ['default', 'query archive', 'query peer', 'explicit'] as const) {
  test(`Take Control locks the ${selection} session displayed in its selector`, async ({ page }) => {
    await prepareWorkspace(page)
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
    const shared = { id: 'shared', title: 'Shared thread', source_channel: 'shared', user_id: 'creator-not-viewer', is_primary: true }
    await page.route(`**/api/agents/${originalAgent.id}/sessions*`, (route) => route.fulfill({ json: route.request().method() === 'POST' ? shared : [
      { id: 'archive', title: 'Another human private primary', source_channel: 'web', is_primary: true },
      shared, { id: 'peer', title: 'Agent peer thread', source_channel: 'agent' },
    ] }))
    const bodies: unknown[] = []
    await page.route(`**/api/agents/${originalAgent.id}/control/*`, async (route) => {
      if (new URL(route.request().url()).pathname.endsWith('/lock')) bodies.push(route.request().postDataJSON())
      await route.fulfill({ json: { status: 'locked', owner_token: 'fixture-control-owner' } })
    })
    const query = selection === 'query archive' ? '?session=archive&env=computer' : selection === 'query peer' ? '?session=peer' : ''
    await page.goto(`/app/agents/${originalAgent.id}/control${query}`)
    await expect(page.getByLabel('Chat session')).toHaveValue(selection === 'query peer' ? 'peer' : 'shared')
    await expect(page.getByRole('option', { name: 'Another human private primary' })).toHaveCount(0)
    if (selection === 'explicit') await page.getByLabel('Chat session').selectOption('peer')

    await page.getByRole('button', { name: 'Take control', exact: true }).click()

    await expect(page.getByLabel('Chat session')).toBeDisabled()
    const expected = selection === 'explicit' || selection === 'query peer' ? 'peer' : 'shared'
    expect(bodies).toEqual([{ session_id: expected, env_type: selection === 'query archive' ? 'computer' : 'browser' }])
    await expect(page).toHaveURL(new RegExp(`session=${expected}`))
  })
}
