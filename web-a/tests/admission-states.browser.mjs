import { bounded, listPath, ordered, responseBarrier, uuid } from './admission-fixture.browser.mjs'
import { button, detached, enabledButton, exact, visible } from './admission-harness.browser.mjs'
import {
  assertDecided, assertList, backToList, decisionCall, detailCall, filterList,
  listAction, listCall, openDetail, reasonField, reviewLink,
} from './admission-actions.browser.mjs'

export async function exerciseStates(h, page, state) {
  // A failed explicit refresh must expose Retry rather than an artificial error page.
  state.listStatus = 503
  let since = state.calls.length
  await page.snapshot(); await button(page, 'Refresh requests').click()
  await state.waitCall(since, listCall())
  await visible(page.locator('main [role="alert"]').filter({ hasText: exact('Synthetic admission list unavailable') }))
  await visible(enabledButton(page, 'Retry'))
  await h.capture(page, state, 'list-error-503')
  state.listStatus = 200; since = state.calls.length
  await page.snapshot(); await button(page, 'Retry').click()
  await state.waitCall(since, listCall())
  await assertList(h, page, state, ordered(state.rows).slice(0, 50))
  await detached(button(page, 'Retry'))

  // Detail loading and GET failure are real navigation/query states on an unseen ID.
  const id = ordered(state.rows)[0].id
  state.detailStatus = 503
  const loading = responseBarrier(state, detailCall(id))
  await page.snapshot(); await reviewLink(page, id).click()
  await bounded(loading.entered, 'Detail request did not enter its loading barrier')
  await visible(page.locator('main [role="status"]').filter({ hasText: exact('Loading admission request...') }))
  h.verify('detail loading cannot submit a decision', await button(page, 'Approve request').count(), 0)
  await h.capture(page, state, 'detail-loading')
  loading.release(); await bounded(loading.completed)
  await visible(page.locator('main [role="alert"]').filter({ hasText: exact('Synthetic admission detail unavailable') }))
  await visible(enabledButton(page, 'Retry'))
  await h.capture(page, state, 'detail-error-503')
  state.detailStatus = 200; since = state.calls.length
  await page.snapshot(); await button(page, 'Retry').click()
  await state.waitCall(since, detailCall(id))
  await visible(enabledButton(page, 'Approve request'))
  await detached(button(page, 'Retry'))

  // A failed decision retains the pending detail, reason draft, and manual retry.
  const reason = 'Synthetic retry draft retained across a 503.\nLongUnbrokenAdmissionReasonForMobileWrapping0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  await reasonField(page).fill(reason)
  state.decisionStatus = 503
  const decision = responseBarrier(state, decisionCall(id))
  await page.snapshot(); await button(page, 'Approve request').click()
  await bounded(decision.entered, 'Decision did not reach the controlled response barrier')
  await visible(button(page, 'Approving...'))
  h.verify('pending decision disables submit', await button(page, 'Approving...').isDisabled())
  h.verify('pending decision disables reason', await reasonField(page).isDisabled())
  await h.capture(page, state, 'detail-submitting')
  decision.release(); await bounded(decision.completed)
  await visible(page.locator('main [role="alert"]').filter({ hasText: exact('Synthetic admission decision unavailable; retry safely.') }))
  await visible(enabledButton(page, 'Approve request'))
  h.verify('503 keeps pending backend record', state.rows.find((row) => row.id === id).status, 'pending')
  h.verify('503 keeps reason draft', await reasonField(page).inputValue(), reason)
  h.verify('503 keeps current detail route', await page.evaluate(() => location.pathname), `/admission-approvals/${id}`)
  h.verify('503 never automatically retries the decision', state.calls.filter(decisionCall(id)).length, 1)
  await h.capture(page, state, 'detail-decision-503-retry')
  state.decisionStatus = 200; since = state.calls.length
  await page.snapshot(); await button(page, 'Approve request').click()
  const retry = await state.waitCall(since, decisionCall(id))
  h.verify('manual retry preserves submitted reason', retry.payload.reason, reason)
  await assertDecided(h, page, id, 'approved')
  await h.capture(page, state, 'detail-retry-approved')
  await backToList(h, page, state)

  // 409 invalidates the detail immediately and the list when it becomes active.
  const root = state.calls.filter(listCall()).at(-1)
  const pageTwo = ordered(state.rows).slice(50, 100)
  await listAction(h, page, state, 'Next', root.response.next_cursor, pageTwo, 2)
  const conflictId = pageTwo[0].id
  await openDetail(page, state, conflictId)
  await reasonField(page).fill('Synthetic losing concurrent decision')
  state.decisionStatus = 409
  const conflictRefresh = responseBarrier(state, detailCall(conflictId))
  since = state.calls.length
  await page.snapshot(); await button(page, 'Reject request').click()
  await state.waitCall(since, decisionCall(conflictId))
  const refreshed = await bounded(conflictRefresh.entered, '409 did not refetch the real detail query')
  await visible(page.locator('main [role="alert"]').filter({ hasText: /This request changed before your decision was saved/u }))
  await h.capture(page, state, 'detail-conflict-refreshing')
  conflictRefresh.release(); await bounded(conflictRefresh.completed)
  await visible(page.locator('main p').filter({ hasText: exact('Synthetic concurrent reviewer decision') }))
  await detached(reasonField(page))
  h.verify('409 renders the server-winning decision', refreshed.response.status, 'approved')
  h.verify('409 detail stays open', await page.evaluate(() => location.pathname), `/admission-approvals/${conflictId}`)
  await h.capture(page, state, 'detail-conflict-resolved')
  state.decisionStatus = 200
  const returning = state.calls.length
  const refreshedList = await backToList(h, page, state)
  h.verify('409 invalidates the pending list on return', state.calls.indexOf(refreshedList) >= returning)
  h.verify('409 refresh excludes resolved record from pending list', refreshedList.response.items.some((row) => row.id === conflictId), false)
  await h.capture(page, state, 'list-after-conflict')

  // Hold an already-snapshotted page-two response, change filter, then release it.
  const cursor = refreshedList.response.next_cursor
  h.verify('late-response test has a next-page cursor', typeof cursor, 'string')
  const late = responseBarrier(state, listCall('pending', cursor))
  await page.snapshot(); await button(page, 'Next').click()
  const lateCall = await bounded(late.entered, 'Next did not enter the late-response barrier')
  h.verify('pending list request uses the held cursor', lateCall.params.cursor, cursor)
  await filterList(h, page, state, 'withdrawn')
  await bounded(late.cancelled, 'Leaving a cursor query did not abort its held HTTP request')
  h.verify('obsolete cursor request was cancelled', lateCall.cancelled)
  late.release(); await bounded(late.completed)
  await page.evaluate(async () => { await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))) })
  await assertList(h, page, state, ordered(state.rows, 'withdrawn').slice(0, 50), 1, 'withdrawn')
  h.verify('released obsolete response never restores pending scope', await page.getByLabel('Request status', { exact: true }).inputValue(), 'withdrawn')
  await h.capture(page, state, 'list-after-cancelled-response')

  const withdrawnId = uuid(101)
  since = state.calls.length
  await page.snapshot(); await reviewLink(page, withdrawnId).click()
  await state.waitCall(since, detailCall(withdrawnId))
  await visible(page.locator('main h2').filter({ hasText: exact('Request details') }))
  await detached(reasonField(page))
  h.verify('withdrawn detail is read-only', await button(page, 'Approve request').count(), 0)
  h.verify('withdrawn detail cannot reject', await button(page, 'Reject request').count(), 0)
  await h.capture(page, state, 'detail-withdrawn')
  await backToList(h, page, state)
  h.verify('every list wire uses only the cursor endpoint', state.calls.filter((call) => call.path.includes('/review')).every((call) => call.path === listPath && !Object.hasOwn(call.params, 'offset')))
}
