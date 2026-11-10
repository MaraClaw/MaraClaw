import { expect, test, type Page } from '@playwright/test'

import { pending, prepareAccess, source, workspaceRequests } from './admission-access-fixture'
import { originalCompany, originalToken, prepareWorkspace } from './session-fencing-fixture'

const community = { name: 'Renamed community', slug: 'renamed-community', is_default_end_user_org: true }
const company = { name: 'KABERS', slug: 'kabers', is_default_end_user_org: false }
const featureApi = /\/api\/(?:okr|org|departments)(?:\/|\?|$)/
const destinations = [
  { path: '/app/okr', heading: 'OKR', content: 'Company objective', endpoint: '/api/okr/objectives/page' },
  { path: '/app/directory', heading: 'Directory', content: 'No people in this company yet.', endpoint: '/api/org/users/page' },
  { path: '/app/directory?tab=departments', heading: 'Directory', content: 'No org departments yet.', endpoint: '/api/departments/options' },
  { path: '/app/OKR', heading: 'OKR', content: 'Company objective', endpoint: '/api/okr/objectives/page' },
  { path: '/app/Directory', heading: 'Directory', content: 'No people in this company yet.', endpoint: '/api/org/users/page' },
] as const

async function prepareOrganization(page: Page, tenant: {
  readonly name: string
  readonly slug: string
  readonly is_default_end_user_org: boolean
}) {
  await prepareWorkspace(page)
  await page.route('**/api/tenants/me', (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    return route.fulfill({ json: { ...originalCompany, ...tenant } })
  })
  const calls: string[] = []
  await page.route(featureApi, (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    calls.push(path)
    expect(request.method()).toBe('GET')
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    switch (path) {
      case '/api/okr/settings': return route.fulfill({ json: {
        enabled: false, period_frequency: 'quarterly', daily_report_enabled: false,
        daily_report_time: '18:00', weekly_report_day: 5, weekly_report_enabled: false,
      } })
      case '/api/okr/periods': return route.fulfill({ json: [
        { start: '2026-10-01', end: '2026-12-31', label: 'Current quarter', is_current: true },
      ] })
      case '/api/okr/objectives/page': return route.fulfill({ json: { items: [
        {
          id: 'company-objective', title: 'Company objective', owner_type: 'company', status: 'active', key_results: [],
          period_start: '2026-10-01', period_end: '2026-12-31', created_at: '2026-10-01T00:00:00Z',
        },
      ], next_cursor: null } })
      case '/api/okr/member-daily-reports':
      case '/api/okr/company-reports': return route.fulfill({ json: [] })
      case '/api/departments/options': return route.fulfill({ json: { departments: [] } })
      case '/api/org/users/page': return route.fulfill({ json: { items: [], next_cursor: null } })
      default: throw new Error(`Unexpected community workspace API request: ${path}`)
    }
  })
  return calls
}

for (const width of [390, 1440]) {
  for (const tenant of [
    { ...community, name: 'KABERS', slug: 'kabers' },
    community,
    company,
  ]) {
    test(`navigation follows default=${tenant.is_default_end_user_org} for ${tenant.slug} at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 })
      await prepareOrganization(page, tenant)

      await page.goto('/app/agents')

      await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible()
      const links = page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')
      await expect(links).toHaveText(tenant.is_default_end_user_org
        ? ['Agents', 'Clawbook', 'Inbox', 'Admissions', 'Settings']
        : ['Agents', 'Clawbook', 'OKR', 'Directory', 'Inbox', 'Admissions', 'Settings'])
      if (tenant.is_default_end_user_org) {
        await expect(page.locator('a[href="/app/okr"], a[href="/app/directory"]')).toHaveCount(0)
      }
      await page.screenshot({ path: testInfo.outputPath('workspace-navigation.png'), fullPage: true })
    })
  }
}

for (const destination of destinations) {
  for (const tenant of [community, company]) {
    test(`direct ${destination.path} follows default=${tenant.is_default_end_user_org}`, async ({ page }) => {
      const calls = await prepareOrganization(page, tenant)

      await page.goto(destination.path)

      if (tenant.is_default_end_user_org) {
        await expect(page).toHaveURL('/app/agents')
        await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible()
        await expect(page.getByRole('heading', { name: destination.heading, exact: true })).toHaveCount(0)
        expect(calls).toEqual([])
      } else {
        await expect(page).toHaveURL(destination.path)
        await expect(page.getByRole('heading', { name: destination.heading, exact: true })).toBeVisible()
        await expect(page.getByText(destination.content, { exact: false })).toBeVisible()
        expect(calls).toContain(destination.endpoint)
      }
    })
  }
}

for (const width of [390, 1440]) {
  for (const destination of destinations.slice(0, 2)) {
    for (const tenant of [community, company]) {
      test(`delayed default=${tenant.is_default_end_user_org} keeps ${destination.path} hidden at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 })
        const calls = await prepareOrganization(page, tenant)
        const requested = Promise.withResolvers<void>()
        const release = Promise.withResolvers<void>()
        await page.route('**/api/tenants/me', async (route) => {
          requested.resolve()
          await release.promise
          await route.fallback()
        })
        const admissionChecked = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/company-admissions/mine/page')

        try {
          await page.goto(destination.path)
          await requested.promise
          await admissionChecked

          await expect(page.getByRole('status', { name: 'Loading workspace' })).toBeVisible()
          await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link', { name: 'Agents', exact: true })).toBeVisible()
          await expect(page.locator('a[href="/app/okr"], a[href="/app/directory"]')).toHaveCount(0)
          await expect(page.getByRole('heading', { name: destination.heading, exact: true })).toHaveCount(0)
          expect(calls).toEqual([])

          release.resolve()
          if (tenant.is_default_end_user_org) {
            await expect(page).toHaveURL('/app/agents')
            await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible()
            expect(calls).toEqual([])
          } else {
            await expect(page).toHaveURL(destination.path)
            await expect(page.getByText(destination.content, { exact: false })).toBeVisible()
            await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link', { name: destination.heading, exact: true })).toBeVisible()
            expect(calls).toContain(destination.endpoint)
          }
        } finally {
          release.resolve()
        }
      })
    }
  }
}

for (const destination of destinations.slice(0, 2)) {
  test(`pending admission keeps priority over the community redirect for ${destination.path}`, async ({ page }) => {
    const { calls } = await prepareAccess(page, pending)
    await page.route('**/api/tenants/me', (route) => route.fulfill({ json: { ...source, is_default_end_user_org: true } }))
    await page.route('**/api/notifications/page?**', (route) => {
      expect(route.request().method()).toBe('GET')
      return route.fulfill({ json: { items: [], next_cursor: null } })
    })
    const visited: string[] = []
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) visited.push(new URL(frame.url()).pathname)
    })

    await page.goto(destination.path)

    await expect(page).toHaveURL('/app/notifications')
    await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeVisible()
    expect(visited).not.toContain('/app/agents')
    expect(workspaceRequests(calls)).toEqual([])
  })
}
