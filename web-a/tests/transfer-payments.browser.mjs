import { button, matrix, runPayments, visible } from './transfer-payment-harness.browser.mjs'
import { bounded, detailPath, listPath } from './transfer-payment-fixture.browser.mjs'

await runPayments(async ({ scenario, capture, verify }) => {
  for (const view of matrix) {
    await scenario({ ...view, name: `payment-${view.width}-${view.theme}`, role: view.width === 390 ? 'org_admin' : 'platform_admin' }, async ({ page, state, go, hold }) => {
      await go(); await visible(page.locator('main a[href^="/transfer-payments/"]'))
      verify('pending queue excludes free requests', await page.locator('main ul li').count(), 1)
      verify('payment nav visible', await page.locator('nav a[href="/transfer-payments"]').count() > 0)
      await capture(page, state, 'queue')
      await page.snapshot(); await page.locator('main a[href^="/transfer-payments/"]').click()
      await visible(page.locator('main input[type="checkbox"]'))
      verify('approval requires payment check', await button(page, 'Confirm payment and create admission').isDisabled())
      await capture(page, state, 'detail')
      await page.snapshot(); await page.locator('main input[type="checkbox"]').check()
      verify('approval enabled after explicit payment check', !await button(page, 'Confirm payment and create admission').isDisabled())
      verify('reason has a visible associated label', await page.evaluate(() => document.querySelector('main textarea').labels[0].textContent.trim()), 'Reason (optional)')
      verify('reduced motion is active', await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches))
      await page.evaluate(() => document.querySelector('main textarea').focus())
      const session = await page.resolveSessionId()
      for (const type of ['keyDown', 'keyUp']) await page.cdp.send('Input.dispatchKeyEvent', { type, key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 }, session)
      verify('keyboard reaches the non-destructive approval before rejection', await page.evaluate(() => document.activeElement.textContent), 'Confirm payment and create admission')
      await capture(page, state, 'ready')
      if (view.width === 390 && view.theme === 'light') {
        const refresh = hold((call) => call.path === detailPath)
        await page.evaluate(() => { document.querySelector('main textarea').focus(); window.__runSyntheticPaymentPoll() })
        await bounded(refresh.entered)
        verify('background polling keeps reason enabled', !await page.locator('main textarea').isDisabled())
        verify('background polling retains keyboard focus', await page.evaluate(() => document.activeElement.tagName), 'TEXTAREA')
        refresh.release(); await bounded(refresh.completed)
      }
      const rejecting = view.theme === 'dark'
      if (rejecting) await page.locator('main textarea').fill('Synthetic missing payment reference')
      const barrier = hold((call) => call.method === 'POST')
      await page.snapshot(); await button(page, rejecting ? 'Reject payment' : 'Confirm payment and create admission').click()
      await bounded(barrier.entered)
      verify('pending locks every decision button', await page.locator('main form button:not(:disabled)').count(), 0)
      verify('pending locks payment check', await page.locator('main input[type="checkbox"]').isDisabled())
      verify('pending locks reason', await page.locator('main textarea').isDisabled())
      await page.evaluate(() => { for (const control of document.querySelectorAll('main form button')) { control.click(); control.click() } })
      verify('double activation sends only one decision', state.calls.filter((call) => call.method === 'POST').length, 1)
      await capture(page, state, 'pending')
      barrier.release()
      await visible(page.locator('main [data-slot="badge"]').filter({ hasText: rejecting ? /^Rejected$/u : /^Admission created$/u }))
      await visible(page.locator('main h2').filter({ hasText: /^Payment review saved:/u }))
      verify('successful decision focuses its visible result', await page.evaluate(() => document.activeElement.textContent.startsWith('Payment review saved:')))
      await capture(page, state, rejecting ? 'rejected' : 'admission-created')
      verify('no destination admission review link', await page.locator('main a[href^="/admission-approvals/"]').count(), 0)
      verify('no destination admission API or identity enrichment', state.calls.every((call) => !call.path.startsWith('/api/users') && !call.path.startsWith('/api/admin/companies') && !/^\/api\/company-admissions\/(?!transfers)/u.test(call.path)))
      if (state.user.role === 'platform_admin') verify('PA needs no tenant lookup', state.calls.filter((call) => call.path === '/api/tenants/me').length, 0)
    })
  }
  for (const width of [390, 1440]) for (const theme of ['light', 'dark']) {
    await scenario({ width, theme, name: `states-${width}-${theme}`, role: 'org_admin' }, async ({ page, state, go, hold }) => {
      const list = hold((call) => call.path === listPath)
      await go(); await bounded(list.entered)
      await visible(page.locator('main [role="status"]').filter({ hasText: 'Loading transfer payments' }))
      await capture(page, state, 'loading'); list.release()
      await visible(page.locator('main a[href^="/transfer-payments/"]'))
      state.listStatus = 503
      await page.snapshot(); await button(page, 'Refresh payments').click()
      await visible(page.locator('main [role="alert"]')); await capture(page, state, 'list-error')
      state.listStatus = 200; state.empty = true
      await page.snapshot(); await button(page, 'Retry').click()
      await visible(page.locator('main h2').filter({ hasText: 'No pending payments' })); await capture(page, state, 'empty')
      await go('/transfer-payments/not-a-uuid'); await visible(page.locator('main [role="alert"]'))
      verify('invalid link issues no detail read', state.calls.filter((call) => call.path === detailPath).length, 0)
      await capture(page, state, 'invalid-link')
      state.detailStatus = 404
      await go(`/transfer-payments/${state.row.id}`); await visible(page.locator('main [role="alert"]'))
      await capture(page, state, 'detail-error')
      state.detailStatus = 200
      await page.snapshot(); await button(page, 'Retry').click(); await visible(page.locator('main input[type="checkbox"]'))
      state.decisionStatus = width === 390 ? 403 : 503
      await page.snapshot(); await button(page, 'Reject payment').click(); await visible(page.locator('main [role="alert"]'))
      await capture(page, state, 'decision-error')
      verify('failed decision not retried', state.calls.filter((call) => call.method === 'POST').length, 1)
      state.decisionStatus = 409
      await page.snapshot(); await button(page, 'Reject payment').click()
      await visible(page.locator('main [data-slot="badge"]').filter({ hasText: /^Admission created$/u }))
      await capture(page, state, 'conflict')
    })
  }
  for (const options of [
    { name: 'other-genesis-denied', role: 'org_admin', defaultOrg: false },
    { name: 'additional-admin-denied', role: 'org_admin', genesis: false },
    { name: 'password-fence', role: 'org_admin', forcePassword: true },
    { name: 'canonical-platform-flag', role: 'org_admin', platformFlag: true },
  ]) {
    await scenario(options, async ({ page, state, go }) => {
      await go()
      if (state.platformFlag) {
        await visible(page.locator('main a[href^="/transfer-payments/"]'))
        verify('canonical PA flag skips tenant lookup', state.calls.filter((call) => call.path === '/api/tenants/me').length, 0)
      } else {
        await visible(page.locator('main h1').filter({ hasText: state.forcePassword ? /^Account$/u : /^Overview$/u }))
        verify('denied reviewer sends no transfer API requests', state.calls.filter((call) => call.path.startsWith('/api/company-admissions/transfers')).length, 0)
        if (state.genesis === false || state.forcePassword) verify('additional admin or force-password sends no tenant lookup', state.calls.filter((call) => call.path === '/api/tenants/me').length, 0)
      }
      await capture(page, state, options.name)
    })
  }
  await scenario({ name: 'tenant-access-error', role: 'org_admin', width: 390, theme: 'dark' }, async ({ page, state, go }) => {
    state.tenantStatus = 503
    await go(); await visible(button(page, 'Retry access check')); await capture(page, state, 'access-error')
    verify('failed capability check sends no transfer reads', state.calls.filter((call) => call.path.startsWith('/api/company-admissions/transfers')).length, 0)
    state.tenantStatus = 200
    await page.snapshot(); await button(page, 'Retry access check').click(); await visible(page.locator('main a[href^="/transfer-payments/"]'))
  })
})
