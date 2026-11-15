import { test, expect } from './fixtures.ts'
import { paymentAliases } from './synthetic-api.ts'

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
