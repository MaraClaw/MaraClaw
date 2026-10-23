// Content-driven synchronization: HTTP receipts plus real controls, never sleeps.
import { listPath, ordered } from './admission-fixture.browser.mjs'
import { button, detached, enabledButton, exact, visible } from './admission-harness.browser.mjs'

export const listCall = (status = 'pending', cursor = null) => (call) => call.method === 'GET' && call.path === listPath && call.params.status === status && (call.params.cursor ?? null) === cursor
export const detailCall = (id) => (call) => call.method === 'GET' && call.path === `/api/company-admissions/${id}`
export const decisionCall = (id) => (call) => call.method === 'POST' && call.path === `/api/company-admissions/${id}/decision`
export const reviewLink = (page, id) => page.locator(`main ul[aria-label="Admission requests"] a[href="/admission-approvals/${id}"]`)
export const reasonField = (page) => page.getByLabel('Reason (optional)', { exact: true })
export async function assertList(h, page, state, rows, number = 1, status = 'pending') {
  if (rows.length) await visible(reviewLink(page, rows[0].id))
  else await visible(page.locator('main h2').filter({ hasText: exact(`No ${status} requests`) }))
  await visible(enabledButton(page, 'Refresh requests'))
  await page.snapshot()
  h.verify('list renders the exact ordered request IDs', await page.locator('main ul[aria-label="Admission requests"] a').evaluateAll((links) => links.map((link) => new URL(link.href).pathname.split('/').at(-1))), rows.map((row) => row.id))
  h.verify('status selector matches server scope', await page.getByLabel('Request status', { exact: true }).inputValue(), status)
  if (rows.length || number > 1) {
    await visible(page.locator('nav[aria-label="Admission request pages"] [role="status"]').filter({ hasText: exact(`Page ${number}`) }))
    h.verify('Previous boundary follows cursor history', await button(page, 'Previous').isDisabled(), number === 1)
  }
  h.verify('list route is active', await page.evaluate(() => location.pathname), '/admission-approvals')
}
export async function listAction(h, page, state, name, cursor, rows, number, status = 'pending') {
  const since = state.calls.length
  await page.snapshot(); await button(page, name).click()
  if (name === 'Refresh requests') await state.waitCall(since, listCall(status, cursor))
  await visible(page.locator('nav[aria-label="Admission request pages"] [role="status"]').filter({ hasText: exact(`Page ${number}`) }))
  await visible(enabledButton(page, 'Refresh requests'))
  const outbound = state.calls.slice(since).filter((call) => call.path === listPath)
  for (const call of outbound) h.verify(`${name} outbound request uses the expected status and opaque cursor`, [call.params.status, call.params.cursor ?? null], [status, cursor])
  const call = state.calls.filter((item) => listCall(status, cursor)(item) && item.status === 200 && !item.cancelled).at(-1)
  h.verify(`${name} has a known synthetic wire response or cache input`, Boolean(call))
  await assertList(h, page, state, outbound.length ? rows : call.response.items, number, status)
  h.receipt.navigation.push({ scenario: state.name, action: name, status, cursor, page: number, source: outbound.length ? 'wire' : 'fresh-cache' })
  return call
}
export async function filterList(h, page, state, status) {
  const since = state.calls.length
  await page.snapshot(); await page.getByLabel('Request status', { exact: true }).selectOption(status)
  await assertList(h, page, state, ordered(state.rows, status).slice(0, 50), 1, status)
  for (const call of state.calls.slice(since).filter((item) => item.path === listPath)) h.verify('filter outbound request resets to its root scope', [call.params.status, call.params.cursor ?? null], [status, null])
  const call = state.calls.filter((item) => listCall(status)(item) && item.status === 200 && !item.cancelled).at(-1)
  h.verify('filter has a known synthetic wire response or cache input', Boolean(call))
  h.verify('filter starts a fresh root cursor', call.params.cursor ?? null, null)
  return call
}
export async function openDetail(page, state, id) {
  const since = state.calls.length
  await page.snapshot(); await reviewLink(page, id).click()
  await state.waitCall(since, detailCall(id))
  await visible(enabledButton(page, 'Approve request'))
}
export async function backToList(h, page, state, history = false) {
  const since = state.calls.length
  await page.snapshot()
  if (history) await page.evaluate(() => window.history.back())
  else await page.locator('main a[href="/admission-approvals"]').click()
  await assertList(h, page, state, ordered(state.rows).slice(0, 50))
  for (const call of state.calls.slice(since).filter((item) => item.path === listPath)) h.verify('return outbound request resets to pending root', [call.params.status, call.params.cursor ?? null], ['pending', null])
  const call = state.calls.filter((item) => listCall()(item) && item.status === 200 && !item.cancelled).at(-1)
  h.verify('return has a known synthetic wire response or cache input', Boolean(call))
  h.verify(`${history ? 'browser Back' : 'Back link'} resets cursor to page one`, call.params.cursor ?? null, null)
  return call
}
export async function assertDecided(h, page, id, status) {
  // Given a completed synthetic decision, inspect the real detail presentation.
  await visible(page.locator('[role="status"]').filter({ hasText: exact(`Request ${status}.`) }))
  await detached(reasonField(page))
  h.verify('successful decision stays on detail', await page.evaluate(() => location.pathname), `/admission-approvals/${id}`)
  h.verify('decided detail cannot be submitted again', await button(page, 'Approve request').count(), 0)
  // When keyboard navigation returns to the Back link, it remains visibly focused.
  await page.locator('main a[href="/admission-approvals"]').evaluate((link) => link.focus())
  await page.keyboard.press('Shift+Tab'); await page.keyboard.press('Tab')
  const presentation = await page.evaluate(() => {
    const link = document.querySelector('main a[href="/admission-approvals"]')
    const icon = link.querySelector('svg')
    const badge = document.querySelector('main [data-slot="badge"]')
    const control = getComputedStyle(link), statusStyle = getComputedStyle(badge)
    const rect = link.getBoundingClientRect(), iconRect = icon.getBoundingClientRect()
    const canvas = document.createElement('canvas'), context = canvas.getContext('2d')
    context.fillStyle = statusStyle.color; context.fillRect(0, 0, 1, 1)
    return {
      status: badge.textContent, color: statusStyle.color, background: statusStyle.backgroundColor,
      rgb: [...context.getImageData(0, 0, 1, 1).data].slice(0, 3),
      text: link.textContent, slot: link.dataset.slot, width: rect.width, height: rect.height,
      radius: parseFloat(control.borderTopLeftRadius), border: control.borderTopWidth,
      alignment: control.alignSelf, decoration: control.textDecorationLine,
      icon: [icon.getAttribute('aria-hidden'), iconRect.width, iconRect.height],
      focused: link.matches(':focus-visible'), shadow: control.boxShadow,
      viewport: innerWidth, theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
    }
  })
  // Then status meaning and the standard labelled, rounded outline control are preserved.
  const [red, green, blue] = presentation.rgb
  h.verify(`${status} detail uses its semantic status color`, status === 'approved' ? green > red * 1.2 && green > blue : red > green * 1.2 && red > blue)
  h.verify('detail Back retains its visible label', presentation.text, 'Back to admission approvals')
  h.verify('detail Back uses the shared outline button', [presentation.slot, presentation.border, presentation.alignment, presentation.decoration], ['button', '1px', 'flex-start', 'none'])
  h.verify('detail Back is a normal rounded rectangle, not an icon circle', presentation.height === 44 && presentation.width > presentation.height * 3 && presentation.radius > 0 && presentation.radius < presentation.height / 2)
  h.verify('detail Back has a decorative 16px arrow', presentation.icon, ['true', 16, 16])
  h.verify('detail Back shows a keyboard focus ring', presentation.focused && presentation.shadow !== 'none')
  if (!h.receipt.detailPresentation) h.receipt.detailPresentation = []
  h.receipt.detailPresentation.push({ id, ...presentation })
}
