/**
 * Standalone synthetic QA, outside npm test/CI. From web-a:
 *   ADMISSIONS_ARTIFACTS=/persistent/evidence node tests/admission-approvals.browser.mjs
 * Existing Chromium only: optional CHROMIUM_EXECUTABLE=/installed/chromium.
 * Uses guarded loadOmowright/connectPipe; refuses any set OMO_BROWSER_ENGINE.
 * Receipts/index.md and PNGs survive owned profile, Vite cache and barrier cleanup.
 */
import { bounded, makeRow, ordered } from './admission-fixture.browser.mjs'
import { button, exact, matrix, runBrowser, visible } from './admission-harness.browser.mjs'
import {
  assertDecided, assertList, backToList, decisionCall, filterList, listAction,
  listCall, openDetail, reasonField,
} from './admission-actions.browser.mjs'
import { exerciseStates } from './admission-states.browser.mjs'

await runBrowser(async (h) => {
  for (const dimensions of matrix) await h.scenario({ ...dimensions, loading: true }, async (page, state) => {
    const original = ordered(structuredClone(state.rows))
    await bounded(state.loadingBarrier.entered, 'List did not reach its loading barrier')
    await visible(page.locator('main [role="status"]').filter({ hasText: exact('Loading admission requests...') }))
    h.verify('loading capture is backed by a held HTTP response', state.loadingBarrier.released, false)
    await h.capture(page, state, 'list-loading')
    state.loadingBarrier.release(); await bounded(state.loadingBarrier.completed)
    await assertList(h, page, state, original.slice(0, 50))
    await h.capture(page, state, 'list-pending-page1')
    const first = state.calls.filter(listCall()).at(-1), cursorOne = first.response.next_cursor
    h.verify('page one returns an opaque cursor', typeof cursorOne, 'string')
    h.verify('cursor fixture exercises reserved-character encoding', /[+/?=]/u.test(cursorOne))

    // Insert above and remove above the anchor between requests. Offset paging
    // would skip original page-two rows; keyset continuation must not.
    state.rows.push(makeRow(102))
    Object.assign(state.rows.find((row) => row.id === original[0].id), {
      status: 'withdrawn', decided_at: '2026-10-22T02:00:00Z', decision_reason: 'Synthetic applicant withdrew before review',
    })
    const second = await listAction(h, page, state, 'Next', cursorOne, original.slice(50, 100), 2)
    const cursorTwo = second.response.next_cursor
    h.verify('equal timestamps retain descending ID tie-break', second.response.items.map((row) => row.id), original.slice(50, 100).map((row) => row.id))
    h.verify('page-two boundary yields another cursor', typeof cursorTwo, 'string')
    await h.capture(page, state, 'list-pending-page2')
    const third = await listAction(h, page, state, 'Next', cursorTwo, original.slice(100), 3)
    h.verify('tail response has no next cursor', third.response.next_cursor, null)
    h.verify('Next is disabled only at the cursor tail', await button(page, 'Next').isDisabled())
    await h.capture(page, state, 'list-pending-tail')
    await listAction(h, page, state, 'Previous', cursorOne, original.slice(50, 100), 2)
    const returned = await listAction(h, page, state, 'Previous', null, ordered(state.rows).slice(0, 50), 1)
    const history = h.receipt.navigation.filter((entry) => entry.scenario === state.name).map((entry) => [entry.action, entry.cursor, entry.page])
    h.verify('Next/Previous observable history preserves the cursor stack', history, [['Next', cursorOne, 2], ['Next', cursorTwo, 3], ['Previous', cursorOne, 2], ['Previous', null, 1]])

    await listAction(h, page, state, 'Next', returned.response.next_cursor, ordered(state.rows).slice(50, 100), 2)
    await listAction(h, page, state, 'Refresh requests', null, ordered(state.rows).slice(0, 50), 1)
    await h.capture(page, state, 'list-explicit-refresh-page1')
    const refreshRoot = state.calls.filter(listCall()).at(-1)
    await listAction(h, page, state, 'Next', refreshRoot.response.next_cursor, ordered(state.rows).slice(50, 100), 2)
    await filterList(h, page, state, 'withdrawn')
    await h.capture(page, state, 'list-filter-reset-withdrawn')
    await filterList(h, page, state, 'approved')
    await h.capture(page, state, 'list-empty-approved')
    await filterList(h, page, state, 'pending')
    await h.capture(page, state, 'list-filter-reset-pending')

    // Both return mechanisms start from page two, not an already-root list.
    for (const { status, browserBack } of [{ status: 'approved', browserBack: false }, { status: 'rejected', browserBack: true }]) {
      const root = state.calls.filter(listCall()).at(-1), rows = ordered(state.rows).slice(50, 100)
      await listAction(h, page, state, 'Next', root.response.next_cursor, rows, 2)
      const id = rows[0].id
      await openDetail(page, state, id)
      const reason = `Synthetic ${status} decision from page two.\nLongUnbrokenAdmissionReviewContext0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ`
      await reasonField(page).fill(reason)
      await h.capture(page, state, `detail-pending-before-${status}`)
      const since = state.calls.length
      await page.snapshot(); await button(page, status === 'approved' ? 'Approve request' : 'Reject request').click()
      const decision = await state.waitCall(since, decisionCall(id))
      h.verify('decision uses the requested operation and retained reason', decision.payload, { decision: status, reason })
      await assertDecided(h, page, id, status)
      await h.capture(page, state, `detail-${status}-success`)
      const returning = state.calls.length
      const reset = await backToList(h, page, state, browserBack)
      h.verify('successful decision invalidates the pending list on return', state.calls.indexOf(reset) >= returning)
      h.verify('resolved page-two request is absent from the refreshed pending root', reset.response.items.some((row) => row.id === id), false)
      await h.capture(page, state, `list-after-${browserBack ? 'browser-back' : 'back-link'}`)
    }
    await exerciseStates(h, page, state)
  })
  h.verify('all six mobile/desktop theme scenarios completed', h.receipt.scenarios.length, matrix.length)
})
