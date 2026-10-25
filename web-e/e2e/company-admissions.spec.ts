import { expect, test, type Page } from '@playwright/test'
import type { AdmissionDetail } from '../src/lib/types/company-admissions'

const tokenKey = 'maraclaw-enduser-token'
const source = { id: 'bdd07e40-83a6-48f3-9049-5ffae4774f75', name: 'OpenClaw', slug: 'openclaw' }
const target = { id: 'c919ad90-90be-4a1d-b699-1d02138b7f45', name: 'Example Company', slug: 'example' }
const member = { id: '47f8d9a9-17eb-4b58-a0e3-fbc6a00d18db', display_name: 'Source member', email: 'member@example.test', tenant_id: source.id, role: 'member', email_verified: true, is_active: true, created_at: '2026-10-25T08:00:00Z' }
const pending: AdmissionDetail = {
  id: 'f68765b7-0926-46c6-804e-3349a5f7e468', status: 'pending', source_tenant_id: source.id, target_tenant: target,
  method: 'invitation', created_at: '2026-10-20T12:00:00Z', decided_at: null, decision_reason: null,
  applicant: { id: member.id, email: member.email }, source_tenant: source, decided_by_user_id: null,
}

async function prepare(page: Page, detail: AdmissionDetail = pending, tenantId: string | null = source.id) {
  const calls: string[] = []
  await page.addInitScript((key) => localStorage.setItem(key, 'source-token'), tokenKey)
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    calls.push(path)
    const targetSession = route.request().headers().authorization === 'Bearer target-token'
    switch (path) {
      case '/api/member/auth/me':
        await route.fulfill({ json: { ...member, tenant_id: targetSession ? target.id : tenantId, display_name: targetSession ? 'Target member' : member.display_name } })
        break
      case '/api/tenants/me': await route.fulfill({ json: targetSession ? target : source }); break
      case '/api/tenants/lookup-by-email': await route.fulfill({ json: { match: target, fallback: source } }); break
      case '/api/tenants/invitations/preview': await route.fulfill({ json: { tenant: { id: target.id, name: target.name } } }); break
      case '/api/company-admissions/transfer-quote': await route.fulfill({ json: { fee_amount_minor: 1000, currency: 'USD', instructions: 'Synthetic instructions', source_eligible: true, has_owned_agents: false, explicit_choice_required: false, free_requires_destructive_consent: true } }); break
      case '/api/company-admissions/transfers/mine': await route.fulfill({ json: [] }); break
      case '/api/company-admissions/mine': await route.fulfill({ json: [detail] }); break
      case '/api/company-admissions/mine/page': await route.fulfill({ json: { items: [detail], next_cursor: null } }); break
      case `/api/company-admissions/${detail.id}`: await route.fulfill({ json: detail }); break
      case '/api/tenants/join-suggested':
      case '/api/tenants/join':
      case '/api/tenants/transfer': await route.fulfill({ status: 202, json: { status: 'pending', request: pending } }); break
      case '/api/auth/switch-tenant': await route.fulfill({ json: { access_token: 'target-token' } }); break
      case '/api/onboarding/status': await route.fulfill({ json: { status: 'completed' } }); break
      case '/api/notifications/unread-count': await route.fulfill({ json: { count: 1 } }); break
      default: await route.fulfill({ json: [] })
    }
  })
  return calls
}

for (const width of [390, 1440]) {
  for (const theme of ['light', 'dark'] as const) {
    test(`detail Back link remains labelled and keyboard-operable at ${width}px in ${theme}`, async ({ page }) => {
      // Given
      await prepare(page)
      await page.setViewportSize({ width, height: 900 })
      await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
      await page.goto(`/app/admissions/${pending.id}`)
      const back = page.getByRole('link', { name: 'All admission requests', exact: true })
      await expect(back).toBeVisible()
      await expect(back).toHaveAttribute('href', '/app/admissions')
      await expect(back.locator('svg[aria-hidden="true"]')).toHaveCount(1)
      await expect(back).toHaveCSS('border-radius', '16px')
      await expect(back).toHaveCSS('border-top-width', '1px')
      expect((await back.boundingBox())?.height).toBeGreaterThanOrEqual(44)
      // When
      await back.focus()
      await expect(back).toBeFocused()
      await expect(back).not.toHaveCSS('box-shadow', 'none')
      await back.press('Enter')
      // Then
      await expect(page).toHaveURL('/app/admissions')
    })

    for (const status of ['pending', 'approved', 'rejected', 'withdrawn'] as const) {
      test(`detail ${status} badge keeps its semantic colour at ${width}px in ${theme}`, async ({ page }) => {
        // Given
        await prepare(page, { ...pending, status })
        await page.setViewportSize({ width, height: 900 })
        await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
        const styles = {
          pending: { color: 'var(--foreground)', background: 'var(--card)', border: 'var(--border)' },
          approved: { color: `var(--color-emerald-${theme === 'dark' ? '300' : '700'})`, background: 'color-mix(in oklab, var(--color-emerald-500) 12%, transparent)', border: 'transparent' },
          rejected: { color: 'var(--destructive)', background: 'color-mix(in oklab, var(--destructive) 12%, transparent)', border: 'transparent' },
          withdrawn: { color: 'var(--foreground)', background: 'var(--card)', border: 'var(--border)' },
        } satisfies Record<AdmissionDetail['status'], Readonly<{ color: string; background: string; border: string }>>
        // When
        await page.goto(`/app/admissions/${pending.id}`)
        // Then
        const badge = page.getByRole('region', { name: 'Admission status' }).locator('[data-slot="badge"]')
        await expect(badge).toBeVisible()
        const colours = await badge.evaluate((element, expected) => {
          const probe = document.createElement('span')
          Object.assign(probe.style, { color: expected.color, backgroundColor: expected.background, borderColor: expected.border })
          document.body.append(probe)
          const actualStyle = getComputedStyle(element)
          const expectedStyle = getComputedStyle(probe)
          const result = {
            actual: [actualStyle.color, actualStyle.backgroundColor, actualStyle.borderTopColor],
            expected: [expectedStyle.color, expectedStyle.backgroundColor, expectedStyle.borderTopColor],
          }
          probe.remove()
          return result
        }, styles[status])
        expect(colours.actual).toEqual(colours.expected)
      })
    }
  }
}

for (const flow of ['invitation join', 'invite transfer'] as const) {
  test(`${flow} persists a pending request without replacing the source session`, async ({ page }) => {
    // Given
    const calls = await prepare(page, pending, flow === 'invitation join' ? null : source.id)
    await page.goto(flow === 'invitation join' ? '/join' : '/transfer')
    await page.getByLabel('Invitation code', { exact: true }).fill('invite')
    await page.getByRole('button', { name: 'Validate invitation', exact: true }).click()
    if (flow === 'invite transfer') await page.getByLabel('Current password').fill('password')
    const button = flow === 'invitation join' ? 'Join organization' : 'Continue transfer'
    // When
    await page.getByRole('button', { name: button, exact: true }).click()
    // Then
    await expect(page.getByRole('status').filter({ hasText: 'Awaiting review' })).toBeVisible()
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
    expect(calls.filter((path) => path === '/api/member/auth/me')).toHaveLength(1)
    await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(0)
    await page.getByRole('link', { name: 'View admission request' }).click()
    await expect(page).toHaveURL(`/app/admissions/${pending.id}`)
    await page.reload()
    await expect(page.getByRole('heading', { name: target.name, exact: true })).toBeVisible()
    await expect(page.getByText('Awaiting review', { exact: true })).toBeVisible()
  })
}

test('a persisted rejection retains the source organization and exposes the decision', async ({ page }) => {
  // Given
  const calls = await prepare(page, { ...pending, status: 'rejected', decision_reason: 'No matching team', decided_at: '2026-10-20T13:00:00Z' })
  // When
  await page.goto('/app/admissions')
  await page.getByRole('link', { name: `View request for ${target.name}` }).click()
  // Then
  await expect(page).toHaveURL(`/app/admissions/${pending.id}`)
  await expect(page.getByText('No matching team', { exact: true })).toBeVisible()
  await expect(page.getByText('Rejected', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: `Open ${target.name}` })).toHaveCount(0)
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
  expect(calls).not.toContain('/api/auth/switch-tenant')
})

test('approval waits for replacement validation before opening the target workspace', async ({ page }) => {
  // Given
  const calls = await prepare(page, { ...pending, status: 'approved', decided_at: '2026-10-20T13:00:00Z' })
  await page.goto(`/app/admissions/${pending.id}`)
  await expect(page.getByText('Approved', { exact: true })).toBeVisible()
  expect(calls).not.toContain('/api/auth/switch-tenant')
  let release = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route('**/api/member/auth/me', async (route) => {
    if (route.request().headers().authorization !== 'Bearer target-token') return route.fallback()
    await gate
    await route.fulfill({ json: { ...member, tenant_id: target.id, display_name: 'Target member' } })
  })
  const validating = page.waitForRequest((request) => request.url().endsWith('/api/member/auth/me') && request.headers().authorization === 'Bearer target-token')
  // When
  await page.getByRole('button', { name: `Open ${target.name}` }).click()
  // Then
  await validating
  await expect(page.getByRole('status').filter({ hasText: 'Loading workspace' })).toBeVisible()
  await expect(page).toHaveURL(`/app/admissions/${pending.id}`)
  release()
  await expect(page).toHaveURL('/app/agents')
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('target-token')
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  await expect(page.getByText('Signed in as Target member.', { exact: false })).toBeVisible()
})

test('an admission notification opens its persisted result and marking read clears the badge', async ({ page }) => {
  // Given
  await prepare(page, { ...pending, status: 'rejected' })
  let read = false
  await page.route('**/api/notifications**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/read')) { read = true; await route.fulfill({ json: { ok: true } }); return }
    if (path.endsWith('/unread-count')) { await route.fulfill({ json: { count: read ? 0 : 1 } }); return }
    await route.fulfill({ json: [{ id: 'notice', title: 'Admission decided', body: null, link: `/app/admissions/${pending.id}`, is_read: read, archived_at: null }] })
  })
  await page.goto('/app/notifications')
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Mark as read: Admission decided', exact: true }).click()
  await page.getByRole('link', { name: 'Open notification: Admission decided', exact: true }).click()
  // Then
  await expect(page).toHaveURL(`/app/admissions/${pending.id}`)
  await expect(page.getByText('Rejected', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Inbox', exact: true }).first()).toBeVisible()
})

test('refreshing a pending request reveals approval without changing the source session', async ({ page }) => {
  // Given
  const calls = await prepare(page)
  await page.goto(`/app/admissions/${pending.id}`)
  await expect(page.getByText('Awaiting review', { exact: true })).toBeVisible()
  await page.route(`**/api/company-admissions/${pending.id}`, (route) => route.fulfill({ json: { ...pending, status: 'approved' } }))
  // When
  await page.getByRole('button', { name: 'Refresh status' }).click()
  // Then
  await expect(page.getByText('Approved', { exact: true })).toBeVisible()
  expect(calls).not.toContain('/api/auth/switch-tenant')
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
})

test('a refused approved-workspace switch leaves the source session usable', async ({ page }) => {
  // Given
  await prepare(page, { ...pending, status: 'approved' })
  await page.route('**/api/auth/switch-tenant', (route) => route.fulfill({ status: 403, json: { detail: 'Membership unavailable' } }))
  await page.goto(`/app/admissions/${pending.id}`)
  // When
  await page.getByRole('button', { name: `Open ${target.name}` }).click()
  // Then
  await expect(page.getByRole('alert')).toHaveText('Membership unavailable')
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
  await expect(page.getByRole('button', { name: `Open ${target.name}` })).toBeEnabled()
})

for (const verified of [true, false]) {
  test(`registration with an admission retains OpenClaw and ${verified ? 'opens its status' : 'preserves email verification'}`, async ({ page }) => {
    // Given
    const calls = await prepare(page)
    await page.addInitScript((key) => localStorage.removeItem(key), tokenKey)
    await page.route('**/api/member/auth/register/init', (route) => route.fulfill({ json: {
      access_token: 'source-token', user: { ...member, email_verified: verified }, admission_request: pending,
      email: member.email, user_id: member.id, message: 'Registration complete',
    } }))
    await page.goto('/register')
    await page.getByLabel('Email', { exact: true }).fill(member.email)
    await page.getByLabel('Password', { exact: true }).fill('password')
    await page.getByLabel('Confirm password', { exact: true }).fill('password')
    await page.getByLabel('Invitation code', { exact: false }).fill('invite')
    // When
    await page.getByRole('button', { name: 'Create account', exact: true }).click()
    // Then
    await expect(page).toHaveURL(verified ? `/app/admissions/${pending.id}` : '/verify-email')
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
    expect(calls).not.toContain('/api/auth/switch-tenant')
    if (verified) await expect(page.getByText('Awaiting review', { exact: true })).toBeVisible()
  })
}
