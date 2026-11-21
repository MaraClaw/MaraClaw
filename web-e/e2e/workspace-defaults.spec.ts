import { expect, test } from '@playwright/test'

import { originalAgent, originalUser, prepareWorkspace } from './session-fencing-fixture'

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
          await route.fulfill({ json: { enabled: false, period_frequency: 'quarterly', daily_report_enabled: false,
            daily_report_time: '18:00', weekly_report_enabled: false, weekly_report_day: 5 } })
          break
        case '/api/okr/objectives/page': {
          const period = url.searchParams.get('period_start') === previous.start ? previous : current
          expect(url.searchParams.get('period_end')).toBe(period.end)
          await route.fulfill({ json: { items: [{ id: period.start, title: `${period.label} objective`, owner_type: 'company',
            period_start: period.start, period_end: period.end, created_at: '2026-10-01T00:00:00Z', status: 'active', key_results: [] }], next_cursor: null } })
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

for (const selection of ['default', 'query read-only', 'query peer', 'explicit', 'created'] as const) {
  test(`Take Control locks the ${selection} session displayed in its selector`, async ({ page }) => {
    await prepareWorkspace(page)
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
    const row = { agent_id: originalAgent.id, user_id: originalUser.id, created_at: '2030-01-01T00:00:00Z' }
    const own = { ...row, id: 'own', title: 'Identity thread', source_channel: 'web', is_primary: true, writable: true }
    const reply = { ...row, id: 'reply', title: 'Scheduled reply', source_channel: 'web', is_primary: false, writable: false }
    const peer = { ...row, id: 'peer', title: 'Agent peer thread', source_channel: 'agent', writable: true }
    let ensures = 0
    await page.route(`**/api/agents/${originalAgent.id}/sessions`, (route) => {
      expect(route.request().method()).toBe('POST')
      ensures++
      return route.fulfill({ json: own })
    })
    await page.route(`**/api/agents/${originalAgent.id}/sessions/page?**`, (route) => {
      expect(new URL(route.request().url()).searchParams.get('scope')).toBe('mine')
      return route.fulfill({ json: { items: selection === 'created' ? [reply, peer] : [reply, own, peer], next_cursor: null, has_more: false } })
    })
    await page.route(`**/api/agents/${originalAgent.id}/credentials/`, (route) => route.fulfill({ json: [] }))
    const bodies: unknown[] = []
    await page.route(`**/api/agents/${originalAgent.id}/control/*`, async (route) => {
      if (new URL(route.request().url()).pathname.endsWith('/lock')) bodies.push(route.request().postDataJSON())
      await route.fulfill({ json: { status: 'locked', owner_token: 'fixture-control-owner' } })
    })
    const query = selection === 'query read-only' ? '?session=reply&env=computer' : selection === 'query peer' ? '?session=peer' : ''
    await page.goto(`/app/agents/${originalAgent.id}/control${query}`)
    await expect(page.getByLabel('Chat session')).toHaveValue(selection === 'query peer' ? 'peer' : 'own')
    await expect(page.getByRole('option', { name: 'Scheduled reply' })).toHaveCount(0)
    await expect(page.getByRole('option', { name: 'Your conversation', exact: true })).toHaveCount(1)
    expect(ensures).toBe(selection === 'created' ? 1 : 0)
    if (selection === 'explicit') await page.getByLabel('Chat session').selectOption('peer')

    await page.getByRole('button', { name: 'Take control', exact: true }).click()

    await expect(page.getByLabel('Chat session')).toBeDisabled()
    const expected = selection === 'explicit' || selection === 'query peer' ? 'peer' : 'own'
    expect(bodies).toEqual([{ session_id: expected, env_type: selection === 'query read-only' ? 'computer' : 'browser' }])
    await expect(page).toHaveURL(new RegExp(`session=${expected}`))
  })
}
