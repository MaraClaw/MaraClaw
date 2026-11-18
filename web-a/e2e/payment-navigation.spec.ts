import type { Page } from '@playwright/test'
import { queuedOrder } from '../tests/capacity-fixtures.ts'
import type { CapacityOrder } from '../src/lib/capacity-api.ts'
import { test, expect } from './fixtures.ts'
import { capacityQueueKey, paymentAliases, type CapacityQueuePage, type syntheticState } from './synthetic-api.ts'

for (const alias of paymentAliases) {
  test(`Payments is current and visible in the rail on ${alias.heading} detail`, async ({ page, isMobile }) => {
    for (const load of ['direct', 'history']) {
      if (load === 'direct') await page.goto(alias.path)
      else {
        await page.getByRole('link', { name: 'Companies', exact: true }).click()
        await expect(page).toHaveURL('/companies')
        await expect(page.getByRole('heading', { name: 'Companies', exact: true })).toBeVisible()
        await page.goBack()
      }
      await expect(page.getByRole('heading', { name: alias.heading, exact: true })).toBeVisible()
      const nav = page.locator(isMobile ? 'header nav' : 'aside nav')
      const payments = nav.getByRole('link', { name: 'Payments', exact: true })
      await expect(payments).toHaveAttribute('aria-current', 'page')
      await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
      await expect(page).toHaveTitle('Payments - MaraClaw Admin')
      if (isMobile) {
        await expect.poll(() => payments.evaluate((link) => {
          const rail = link.closest('nav')
          if (!rail) throw new Error('Payments link must belong to the navigation rail')
          const bounds = rail.getBoundingClientRect()
          const target = link.getBoundingClientRect()
          return {
            fullyVisible: target.left >= bounds.left - 1 && target.right <= bounds.right + 1,
            left: target.left, right: target.right, railLeft: bounds.left, railRight: bounds.right,
            scrollLeft: rail.scrollLeft,
          }
        }), { message: `Payments rail after ${load} navigation` }).toMatchObject({ fullyVisible: true })
      }
    }
  })
}

test.describe('genesis platform admin payment sections', () => {
  test.use({ actor: 'genesis_platform_admin' })

  test('all four sections stay under Payments and only the selected panel loads', async ({ page, api }) => {
    await page.goto('/payments')
    const sections = page.getByRole('tablist', { name: 'Payment sections', exact: true })
    await expect(sections.getByRole('tab')).toHaveText(['Organization transfers', 'Extra accounts and agents', 'Organization admissions', 'Payment settings'])
    await expect(page.getByRole('tabpanel', { name: 'Organization transfers', exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Org admissions', exact: true })).toHaveCount(0)
    expect(api.requests.filter((request) => request.path.startsWith('/api/admin/organization-admissions') || request.path === '/api/admin/capacity-payment-settings')).toEqual([])

    await sections.getByRole('tab', { name: 'Extra accounts and agents', exact: true }).click()
    await expect(page).toHaveURL('/payments?tab=capacity')
    await expect(page.getByRole('heading', { name: 'No payments with this status', exact: true })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Payment settings', exact: true })).toHaveCount(0)
    expect(api.requests.filter((request) => request.path === '/api/admin/capacity-payment-settings')).toEqual([])

    const admissions = sections.getByRole('tab', { name: 'Organization admissions', exact: true })
    await admissions.click()
    await expect(page).toHaveURL('/payments?tab=admissions')
    await expect(page.getByRole('button', { name: 'Review request from founder@example.test', exact: true })).toBeVisible()
    await expect(admissions).toBeFocused()
    await expect(page.locator('main h1')).toHaveText('Payments')
    await sections.getByRole('tab', { name: 'Payment settings', exact: true }).click()
    await expect(page).toHaveURL('/payments?tab=settings')
    await expect(page.getByLabel('Bank name', { exact: true })).toHaveValue('Example Bank')
    await expect(page.getByRole('tabpanel')).toHaveCount(1)
    await expect(page.getByRole('link', { name: 'Payments', exact: true })).toHaveAttribute('aria-current', 'page')
  })

  test('legacy admissions links redirect and the embedded review still approves requests', async ({ page, api }) => {
    await page.goto('/organization-admissions')
    await expect(page).toHaveURL('/payments?tab=admissions')
    await expect(page.getByRole('tab', { name: 'Organization admissions', exact: true })).toHaveAttribute('aria-selected', 'true')
    await page.getByRole('button', { name: 'Review request from founder@example.test', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Example Organization', exact: true })).toBeFocused()
    await expect(page.getByRole('button', { name: 'Approve request', exact: true })).toBeDisabled()
    await page.getByRole('checkbox', { name: /^I checked that/ }).check()
    await page.getByLabel('Review note (optional)', { exact: true }).fill('Bank transfer confirmed')
    await page.getByRole('button', { name: 'Approve request', exact: true }).click()
    await expect(page.getByText('Request approved.', { exact: true })).toBeVisible()
    expect(api.requests.filter((request) => request.method === 'POST')).toEqual([
      { method: 'POST', path: `/api/admin/organization-admissions/${api.admission.id}/decision`, body: { decision: 'approve', note: 'Bank transfer confirmed' } },
    ])
    await page.getByRole('button', { name: 'Back to requests', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Organization admissions', exact: true })).toBeFocused()
    await expect(page.getByText('No pending requests. New requests will appear here after submission.', { exact: true })).toBeVisible()
  })

  test('payment settings load and save only in their own tab', async ({ page, api }) => {
    await page.goto('/payments?tab=settings')
    await expect(page.getByLabel('Bank name', { exact: true })).toHaveValue('Example Bank')
    await expect(page.getByRole('heading', { name: 'Payment settings', exact: true })).toBeVisible()
    await expect(page.getByLabel('Payment status', { exact: true })).toHaveCount(0)
    expect(api.requests.filter((request) => request.path === '/api/admin/capacity-orders')).toEqual([])
    await page.getByLabel('Bank name', { exact: true }).fill('Updated Bank')
    await page.getByLabel('VND for 1 USD', { exact: true }).fill('26000')
    await page.getByRole('button', { name: 'Save payment settings', exact: true }).click()
    await expect(page.getByText('Payment settings saved', { exact: true })).toBeVisible()
    await expect(page.getByText('Saved rate: 26,000 VND per USD.', { exact: true })).toBeVisible()
    expect(api.requests.filter((request) => request.method === 'PUT')).toEqual([
      { method: 'PUT', path: '/api/admin/capacity-payment-settings', body: {
        bank_name: 'Updated Bank', account_number: '0123456789', branch_name: 'District 1 Branch', usd_vnd_rate: 26000,
      } },
    ])
    await page.reload()
    await expect(page.getByLabel('Bank name', { exact: true })).toHaveValue('Updated Bank')
    await expect(page.getByLabel('VND for 1 USD', { exact: true })).toHaveValue('26000')
  })
})

test('regular platform admins fall back from forbidden query tabs without restricted reads', async ({ page, api }) => {
  for (const tab of ['admissions', 'settings', 'unknown']) {
    await page.goto(`/payments?tab=${tab}`)
    await expect(page.getByRole('tab')).toHaveText(['Organization transfers', 'Extra accounts and agents'])
    await expect(page.getByRole('tab', { name: 'Organization transfers', exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByRole('heading', { name: 'No pending payments', exact: true })).toBeVisible()
    await expect(page.getByRole('tabpanel')).toHaveCount(1)
    await expect(page.getByLabel('Bank name', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Review request from founder@example.test', exact: true })).toHaveCount(0)
  }
  expect(api.requests.filter((request) => request.path.startsWith('/api/admin/organization-admissions') || request.path === '/api/admin/capacity-payment-settings')).toEqual([])
})

for (const actor of ['platform_admin', 'genesis_platform_admin'] as const) test.describe(`${actor} keyboard traversal`, () => {
  test.use({ actor })
  test('arrow keys wrap and Home/End select only eligible sections without losing focus', async ({ page }) => {
    await page.goto('/payments')
    const labels = actor === 'genesis_platform_admin'
      ? ['Organization transfers', 'Extra accounts and agents', 'Organization admissions', 'Payment settings']
      : ['Organization transfers', 'Extra accounts and agents']
    const first = page.getByRole('tab', { name: labels[0], exact: true })
    await first.focus()
    for (const label of [...labels.slice(1), labels[0]]) {
      await page.keyboard.press('ArrowRight')
      const tab = page.getByRole('tab', { name: label, exact: true })
      await expect(tab).toHaveAttribute('aria-selected', 'true')
      await expect(tab).toBeFocused()
      await expect(tab).toBeInViewport()
    }
    for (const key of ['ArrowLeft', 'End', 'ArrowUp']) {
      await page.keyboard.press('Home')
      await expect(first).toHaveAttribute('aria-selected', 'true')
      await expect(first).toBeFocused()
      await page.keyboard.press(key)
      const last = page.getByRole('tab', { name: labels.at(-1), exact: true })
      await expect(last).toHaveAttribute('aria-selected', 'true')
      await expect(last).toBeFocused()
    }
    await page.keyboard.press('Home')
    await expect(first).toHaveAttribute('aria-selected', 'true')
    await expect(first).toBeFocused()
    await page.keyboard.press('ArrowDown')
    await expect(page.getByRole('tab', { name: 'Extra accounts and agents', exact: true })).toBeFocused()
    await expect(page.getByRole('tab', { selected: true })).toHaveText('Extra accounts and agents')
  })
})

test.describe('capacity payment queue paging', () => {
  const a1 = queuedOrder(1)
  const a2 = queuedOrder(2)
  const b1 = queuedOrder(3)
  const b2 = queuedOrder(4)
  const c1 = queuedOrder(5)
  const queue = '/payments?tab=capacity'

  function script(api: ReturnType<typeof syntheticState>, status: string, pages: readonly (readonly [string | null, readonly CapacityOrder[], string | null])[]) {
    for (const [cursor, items, next] of pages) api.capacityQueue.set(capacityQueueKey(status, cursor), { items, next_cursor: next } satisfies CapacityQueuePage)
  }
  function controls(page: Page) {
    const pages = page.getByRole('navigation', { name: 'Capacity payment pages', exact: true })
    return {
      previous: pages.getByRole('button', { name: 'Previous', exact: true }), next: pages.getByRole('button', { name: 'Next', exact: true }),
      label: pages.getByRole('status'),
    }
  }
  const expectQueue = (page: Page, ...orders: readonly CapacityOrder[]) => expect(
    page.getByRole('list', { name: 'Capacity payments', exact: true }).getByRole('listitem'),
  ).toContainText(orders.map((order) => order.reference))

  test('Next follows the server cursor and Previous revisits each visited boundary', async ({ page, api }) => {
    script(api, 'submitted', [[null, [a1, a2], 'cursor-page-2'], ['cursor-page-2', [b1, b2], 'cursor-page-3'], ['cursor-page-3', [c1], null]])
    await page.goto(queue)
    const { previous, next, label } = controls(page)
    await expectQueue(page, a1, a2)
    await expect(label).toHaveText('Page 1')
    await expect(previous).toBeDisabled()
    await expect(next).toBeEnabled()

    await next.click()
    await expectQueue(page, b1, b2)
    await expect(label).toHaveText('Page 2')
    await expect(previous).toBeEnabled()
    await next.click()
    await expectQueue(page, c1)
    await expect(label).toHaveText('Page 3')
    await expect(next).toBeDisabled()

    await previous.click()
    await expectQueue(page, b1, b2)
    await expect(label).toHaveText('Page 2')
    await previous.click()
    await expectQueue(page, a1, a2)
    await expect(label).toHaveText('Page 1')
    await expect(previous).toBeDisabled()
    expect(new Set(api.capacityQueueRequests.map((request) => request.cursor))).toEqual(new Set([null, 'cursor-page-2', 'cursor-page-3']))
    expect(api.capacityQueueRequests.every((request) => request.offset === null && request.limit === '50')).toBe(true)
  })

  test('Previous is offered on a later page that emptied between clicks and returns to the known boundary', async ({ page, api }) => {
    script(api, 'submitted', [[null, [a1, a2], 'cursor-page-2'], ['cursor-page-2', [b1], null]])
    await page.goto(queue)
    const { previous, next, label } = controls(page)
    await expectQueue(page, a1, a2)

    script(api, 'submitted', [['cursor-page-2', [], null]])
    await next.click()
    await expect(page.getByRole('heading', { name: 'No payments with this status', exact: true })).toBeVisible()
    await expect(page.getByText('Return to the previous page to see earlier payments.', { exact: true })).toBeVisible()
    await expect(label).toHaveText('Page 2')
    await expect(next).toBeDisabled()
    await expect(previous).toBeEnabled()

    await previous.click()
    await expectQueue(page, a1, a2)
    await expect(label).toHaveText('Page 1')
    await expect(previous).toBeDisabled()
  })

  test('Refresh starts a new traversal from a fresh first page', async ({ page, api }) => {
    script(api, 'submitted', [[null, [a1, a2], 'cursor-page-2'], ['cursor-page-2', [b1], null]])
    await page.goto(queue)
    const { previous, next, label } = controls(page)
    await expectQueue(page, a1, a2)
    await next.click()
    await expectQueue(page, b1)
    await expect(label).toHaveText('Page 2')

    script(api, 'submitted', [[null, [c1, a1], 'cursor-fresh'], ['cursor-fresh', [a2, b1], null]])
    await page.getByRole('button', { name: 'Refresh payments', exact: true }).click()
    await expectQueue(page, c1, a1)
    await expect(label).toHaveText('Page 1')
    await expect(previous).toBeDisabled()

    await next.click()
    await expectQueue(page, a2, b1)
    await expect(label).toHaveText('Page 2')
    expect(api.capacityQueueRequests.at(-1)).toMatchObject({ status: 'submitted', cursor: 'cursor-fresh', offset: null })
  })

  test('changing the status starts a new traversal and never sends the old status its cursor', async ({ page, api }) => {
    script(api, 'submitted', [[null, [a1], 'cursor-page-2'], ['cursor-page-2', [b1], null]])
    script(api, 'approved', [[null, [c1], null]])
    await page.goto(queue)
    const { previous, next, label } = controls(page)
    await expectQueue(page, a1)
    await next.click()
    await expectQueue(page, b1)
    await expect(label).toHaveText('Page 2')

    const status = page.getByLabel('Payment status', { exact: true })
    await status.selectOption('approved')
    await expectQueue(page, c1)
    await expect(label).toHaveText('Page 1')
    await expect(previous).toBeDisabled()
    await expect(next).toBeDisabled()

    await status.selectOption('submitted')
    await expectQueue(page, a1)
    await expect(label).toHaveText('Page 1')
    await expect(previous).toBeDisabled()
    await expect(next).toBeEnabled()
    expect(api.capacityQueueRequests.filter((request) => request.status === 'approved').map((request) => request.cursor)).toEqual([null])
  })
})
