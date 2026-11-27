import type { CompanyRuntimeWorkspaceOut } from '../src/lib/engine-contracts.generated.ts'
import { runtimePage } from '../tests/company-operations-fixtures.ts'
import { test, expect } from './fixtures.ts'

const [healthy] = runtimePage.items
const failedAt = '2026-11-15T04:00:00Z'
const requestedAt = '2026-11-15T05:00:00Z'
const agent = (number: number, name: string, workspace: CompanyRuntimeWorkspaceOut | null) => ({
  ...healthy, agent_id: `22222222-2222-4222-8222-${String(number).padStart(12, '0')}`, name, workspace,
})
const agents = [
  agent(1, 'Failed Walk Agent', { ...healthy.workspace, used_bytes: 2 * 1024 ** 2, walk_failed_at: failedAt }),
  agent(2, 'Re-measure Agent', { ...healthy.workspace, remeasure_requested_at: requestedAt }),
  agent(3, 'Never Measured Agent', { ...healthy.workspace, measured_at: null, walk_failed_at: failedAt }),
  agent(4, 'Untracked Agent', null),
  agent(5, 'Measured Agent', healthy.workspace),
]
const runtimePath = `/companies/${runtimePage.company_id}/runtimes`
const card = (page: import('@playwright/test').Page, name: string) => page.getByRole('article', { name, exact: true })
const term = (page: import('@playwright/test').Page, name: string, label: string) =>
  card(page, name).getByRole('term').getByText(label, { exact: true })
const field = (page: import('@playwright/test').Page, name: string, label: string) => term(page, name, label).locator('xpath=following-sibling::dd[1]')

test.beforeEach(({ api }) => { api.runtime = { ...runtimePage, items: agents } })

test('each agent card states its workspace measurement in words, with times like the neighbouring fields', async ({ page, api }, testInfo) => {
  await page.goto(runtimePath)
  await expect(page.getByRole('heading', { name: 'Runtime inventory', exact: true })).toBeVisible()
  const statuses = (name: string) => card(page, name).getByRole('listitem')
  await expect(statuses('Failed Walk Agent')).toHaveText(['Last measurement failed at Nov 15, 2026, 4:00 AM'])
  await expect(statuses('Re-measure Agent')).toHaveText(['Last measurement succeeded', 'Re-measure requested at Nov 15, 2026, 5:00 AM'])
  await expect(statuses('Never Measured Agent')).toHaveText(['Last measurement failed at Nov 15, 2026, 4:00 AM', 'Never measured'])
  await expect(statuses('Untracked Agent')).toHaveText(['Never measured'])
  await expect(statuses('Measured Agent')).toHaveText(['Last measurement succeeded'])
  // A failed walk keeps the last stored sizes; a never-measured row shows only its real limit.
  await expect(field(page, 'Failed Walk Agent', 'Workspace used / limit')).toHaveText('2 MiB / 1 GiB')
  await expect(field(page, 'Failed Walk Agent', 'Workspace measured')).toHaveText('Nov 15, 2026, 3:00 AM')
  await expect(field(page, 'Never Measured Agent', 'Workspace used / limit')).toHaveText('Unknown / 1 GiB')
  await expect(field(page, 'Never Measured Agent', 'Workspace measured')).toHaveText('None')
  await expect(field(page, 'Untracked Agent', 'Workspace used / limit')).toHaveText('Unknown')
  // Icons only repeat the words, and no status is clipped or pushes the page sideways.
  const badges = page.locator('[data-slot="badge"]')
  await expect(badges).toHaveCount(7)
  expect(await badges.evaluateAll((elements) => elements.every((badge) => badge.scrollWidth <= badge.clientWidth
    && [...badge.querySelectorAll('svg')].every((icon) => icon.getAttribute('aria-hidden') === 'true')))).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  for (const name of ['Failed Walk Agent', 'Re-measure Agent', 'Never Measured Agent']) {
    await term(page, name, 'Workspace measurement status').locator('xpath=..').screenshot({ path: testInfo.outputPath(`${name}.png`) })
  }
  expect(new Set(api.requests.filter((request) => request.path.endsWith('/runtime')).map((request) => request.path)))
    .toEqual(new Set([`/api/admin/companies/${runtimePage.company_id}/runtime`]))
})

test('the measurement statuses read in Vietnamese', async ({ page }, testInfo) => {
  await page.addInitScript(() => window.localStorage.setItem('maraclaw-admin-language', 'vi'))
  await page.goto(runtimePath)
  const status = term(page, 'Never Measured Agent', 'Trạng thái đo không gian làm việc')
  await expect(status).toBeVisible()
  await expect(card(page, 'Never Measured Agent').getByRole('listitem')).toHaveText(['Lần đo gần nhất thất bại lúc 04:00 15 thg 11, 2026', 'Chưa từng đo'])
  await expect(card(page, 'Re-measure Agent').getByRole('listitem')).toHaveText(['Lần đo gần nhất thành công', 'Đã yêu cầu đo lại lúc 05:00 15 thg 11, 2026'])
  await expect(field(page, 'Never Measured Agent', 'Dung lượng không gian làm việc đã dùng / giới hạn')).toHaveText('Chưa biết / 1 GiB')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await status.locator('xpath=..').screenshot({ path: testInfo.outputPath('never-measured-vi.png') })
})
