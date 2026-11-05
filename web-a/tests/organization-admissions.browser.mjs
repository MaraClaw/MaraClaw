// Run from web-a: ORGANIZATION_ADMISSIONS_ARTIFACTS=<persistent directory> BROWSER_SKILL_ROOT=<browser skill> node tests/organization-admissions.browser.mjs
// QA_MODE=before captures the unchanged UI with the same synthetic payloads.
import { button, enabledButton, exact, runBrowser, visible } from './organization-admission-harness.browser.mjs'
import { bounded, responseBarrier } from './organization-admission-fixture.browser.mjs'

await runBrowser(async ({ scenario, capture, verify, receipt }) => {
  for (const width of [390, 1440]) for (const theme of ['light', 'dark']) await scenario({ width, theme }, async (page, state) => {
    const review = (number) => page.locator(`button[aria-label="Review request from founder-${number}@example.test"]`)
    await visible(review(1)); await capture(page, state, 'queue')
    if (receipt.mode !== 'before') {
      verify('queue displays organization name', await page.evaluate(() => document.querySelector('main h2')?.textContent), 'Synthetic International Operations')
      verify('queue displays explicit legacy name absence', await page.evaluate(() => [...document.querySelectorAll('main h2')].some((heading) => heading.textContent === 'Organization name not provided')))
      await enabledButton(page, 'Next').click()
      await visible(page.locator('[role="status"]').filter({ hasText: exact('Page 2') }))
      verify('next page uses offset fifty', state.calls.some((call) => call.params.offset === '50'))
      await page.locator('main select').selectOption('approved')
      await visible(review(3))
      verify('filter returns to page one', state.calls.some((call) => call.params.status === 'approved' && call.params.offset === '0'))
      await page.locator('main select').selectOption('pending'); await visible(review(1))
    }
    await review(1).focus(); await review(1).press('Enter')
    await visible(button(page, 'Back to requests')); await capture(page, state, 'detail')
    if (receipt.mode !== 'before') {
      verify('submitted organization is detail heading', await page.evaluate(() => document.querySelector('main h2')?.textContent), 'Synthetic International Operations')
      verify('applicant email remains visible', await page.evaluate(() => document.querySelector('main')?.textContent.includes('founder-1@example.test')))
      verify('detail heading receives keyboard focus', await page.evaluate(() => document.activeElement === document.querySelector('main h2')))
      verify('new quote renders its per-account cap', await page.evaluate(() => [...document.querySelectorAll('main dl > div')].map((field) => [field.querySelector('dt')?.textContent, field.querySelector('dd')?.textContent]).find(([label]) => label === 'Maximum agents per account')), ['Maximum agents per account', '5'])
    }
    await button(page, 'Back to requests').click(); await visible(review(2)); await review(2).click()
    await visible(button(page, 'Approve request')); await capture(page, state, 'legacy')
    if (receipt.mode !== 'before') {
      verify('legacy detail names absence', await page.evaluate(() => document.querySelector('main h2')?.textContent), 'Organization name not provided')
      verify('unnamed request retains current per-account cap', await page.evaluate(() => [...document.querySelectorAll('main dl > div')].map((field) => [field.querySelector('dt')?.textContent, field.querySelector('dd')?.textContent]).find(([label]) => label === 'Maximum agents per account')), ['Maximum agents per account', '5'])
      verify('legacy approve disabled', await page.evaluate(() => document.querySelector('button[type="submit"]')?.disabled))
      await visible(enabledButton(page, 'Reject request'))
      const before = state.calls.filter((call) => call.method === 'POST').length
      await page.evaluate(() => document.querySelector('main form').requestSubmit())
      await button(page, 'Reject request').focus(); await button(page, 'Reject request').press('Enter')
       await visible(page.locator('[role="status"]').filter({ hasText: exact('Request rejected.') }))
       await capture(page, state, 'rejected')
      verify('rejection has no credentials delivery section', await page.evaluate(() => ![...document.querySelectorAll('main h3')].some((el) => el.textContent === 'Initial credentials email')))
      verify('legacy implicit submit refused and rejection sent once', state.calls.filter((call) => call.method === 'POST').length, before + 1)
      verify('only legacy rejection transported', state.calls.filter((call) => call.method === 'POST').at(-1)?.payload.decision, 'reject')
    }
    await button(page, 'Back to requests').click(); await visible(review(1)); await review(1).click()
    state.decisionStatus = 409
    await enabledButton(page, 'Approve request').click(); await visible(page.locator('[role="alert"]'))
    await capture(page, state, 'conflict')
    if (receipt.mode !== 'before') {
      verify('conflict preserves server cause', await page.evaluate(() => document.querySelector('[role="alert"]')?.textContent.includes('An organization with this name already exists.')))
      verify('conflict never mislabels provisioning as race', await page.evaluate(() => !document.querySelector('main')?.textContent.includes('This request has already changed.')))
    }
    await button(page, 'Back to requests').click(); await visible(review(1))
    if (receipt.mode !== 'before') {
      verify('back restores queue heading focus', await page.evaluate(() => document.activeElement === document.querySelector('main h1')))
      await button(page, 'Refresh requests').click(); await visible(review(1))
      state.decisionStatus = 200
      await review(1).click(); await enabledButton(page, 'Approve request').click()
      await visible(page.locator('[role="status"]').filter({ hasText: exact('Request approved.') })); await capture(page, state, 'approved')
      await button(page, 'Back to requests').click()
      await page.locator('main select').selectOption('approved'); await visible(review(3))
      for (const [number, status] of [[3, 'pending'], [4, 'sent'], [5, 'failed']]) {
        await review(number).click(); await visible(page.locator('main h3').filter({ hasText: exact('Initial credentials email') }))
        verify('delivery detail keeps approved status', await page.evaluate(() => document.querySelector('main [data-slot="badge"]')?.textContent), 'Approved')
        verify('delivery status belongs to original applicant', await page.evaluate((id) => document.querySelector('main')?.textContent.includes(`founder-${id}@example.test`), number))
        if (status !== 'failed') {
          verify('queued and SMTP accepted have no retry action', await page.evaluate(() => ![...document.querySelectorAll('main button')].some((el) => el.textContent === 'Retry credentials email')))
          await visible(page.locator('[role="status"]').filter({ hasText: status === 'pending' ? /queued for delivery/u : /does not guarantee inbox delivery/u }))
        } else {
          await visible(enabledButton(page, 'Retry credentials email'))
          await capture(page, state, 'email-failed')
          for (const failure of [503, 409]) {
            state.retryStatus = failure
            await enabledButton(page, 'Retry credentials email').click(); await visible(page.locator('[role="alert"]'))
            verify('retry error keeps approved decision', await page.evaluate(() => document.querySelector('main [data-slot="badge"]')?.textContent), 'Approved')
            if (failure === 409) verify('obsolete credentials refusal shows server text', await page.evaluate(() => document.querySelector('[role="alert"]')?.textContent), 'The administrator has changed the password. Original credentials cannot be resent.')
            await capture(page, state, `email-retry-error-${failure}`)
          }
          state.retryStatus = 200
          const barrier = responseBarrier(state, (call) => call.path.endsWith('/retry-email'))
          await enabledButton(page, 'Retry credentials email').focus(); await button(page, 'Retry credentials email').press('Enter')
          await bounded(barrier.entered)
          await visible(page.locator('[role="status"]').filter({ hasText: exact('Queueing credentials email...') }))
          verify('pending retry locks Back', await page.evaluate(() => [...document.querySelectorAll('main button')].find((el) => el.textContent === 'Back to requests')?.disabled))
          verify('pending retry locks only its action without a decision form', await page.evaluate(() => !document.querySelector('main form') && [...document.querySelectorAll('main button')].find((el) => el.textContent === 'Retry credentials email')?.disabled))
          await capture(page, state, 'email-retry-pending'); barrier.release()
          await visible(page.locator('[role="status"]').filter({ hasText: /queued for delivery/u }))
          verify('successful retry preserves original request', await page.evaluate(() => document.querySelector('main h2')?.textContent), 'Synthetic Delivery failed')
          verify('successful retry retains review note', await page.evaluate(() => document.querySelector('main')?.textContent.includes('Approved synthetic request')))
        }
        await capture(page, state, status === 'failed' ? 'email-requeued' : `email-${status}`)
        await button(page, 'Back to requests').click(); await visible(review(3))
      }
      await page.locator('main select').selectOption('pending')
      await visible(page.locator('[role="status"]').filter({ hasText: exact('No pending requests. New requests will appear here after submission.') }))
      state.listStatus = 503
      await button(page, 'Refresh requests').click()
      await visible(page.locator('[role="alert"]').filter({ hasText: exact('Synthetic organization queue unavailable') }))
      state.listStatus = 200
      await button(page, 'Retry').click()
      await visible(page.locator('[role="status"]').filter({ hasText: exact('No pending requests. New requests will appear here after submission.') }))
    }
  })
})
