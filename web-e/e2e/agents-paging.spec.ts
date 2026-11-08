import { expect, test } from '@playwright/test'
import { delayedAgent, originalAgent, originalToken, prepareWorkspace, replacementAgent, replacementToken, switchFromSettings, writeFromOtherTab } from './session-fencing-fixture'

const cursor = 'opaque+/=? page&company=other'

for (const width of [390, 1440]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`directory ${width} ${colorScheme}`, () => {
      test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })
      test('Load more retains cards on failure and explicitly retries the opaque cursor without duplicates', async ({ page }, info) => {
        await prepareWorkspace(page)
        const continuations: string[] = []
        await page.route('**/api/agents/page?*', async (route) => {
          const params = new URL(route.request().url()).searchParams
          expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
          const next = params.get('cursor')
          if (params.get('limit') === '1') return route.fulfill({ json: { items: [originalAgent], next_cursor: cursor, has_more: true } })
          if (next === null) return route.fulfill({ json: { items: [originalAgent], next_cursor: cursor, has_more: true } })
          continuations.push(next)
          if (continuations.length === 1) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
          return route.fulfill({ json: { items: [originalAgent, { ...delayedAgent, created_at: null }], next_cursor: null, has_more: false } })
        })
        await page.goto('/app/agents')
        await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()
        expect(continuations).toEqual([])
        await page.getByRole('button', { name: 'Load more', exact: true }).click()
        await expect(page.getByRole('button', { name: 'Retry loading more' })).toBeVisible()
        await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toHaveCount(1)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await page.screenshot({ path: info.outputPath('continuation-error.png') })
        await page.getByRole('button', { name: 'Retry loading more' }).click()
        await expect(page.getByRole('link', { name: new RegExp(delayedAgent.name) })).toBeVisible()
        await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toHaveCount(1)
        await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0)
        for (const card of await page.locator('main a[href$="/chat"]').all()) {
          expect(await card.evaluate((node) => node.getBoundingClientRect().right <= innerWidth)).toBe(true)
        }
        expect(continuations).toEqual([cursor, cursor])
        await page.screenshot({ path: info.outputPath('continuation-loaded.png') })
      })
    })
  }
}

for (const transition of ['logout', 'replacement', 'switch'] as const) {
  test(`late continuation cannot restore cards after ${transition}`, async ({ page }) => {
    await prepareWorkspace(page)
    const entered = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route('**/api/agents/page?*', async (route) => {
      const request = route.request()
      const params = new URL(request.url()).searchParams
      if (request.headers().authorization === `Bearer ${replacementToken}`) {
        return route.fulfill({ json: { items: [replacementAgent], next_cursor: null, has_more: false } })
      }
      if (params.has('cursor')) {
        entered.resolve()
        await release.promise
        return route.fulfill({ json: { items: [delayedAgent], next_cursor: null, has_more: false } })
      }
      return route.fulfill({ json: { items: [originalAgent], next_cursor: cursor, has_more: true } })
    })
    try {
      await page.goto('/app/agents')
      await page.getByRole('button', { name: 'Load more', exact: true }).click()
      await entered.promise
      switch (transition) {
        case 'logout': await page.getByRole('button', { name: 'Sign out' }).click(); break
        case 'replacement': await writeFromOtherTab(page, replacementToken); break
        case 'switch': await switchFromSettings(page); break
      }
      if (transition === 'logout') await expect(page).toHaveURL('/login')
      else {
        await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
        await expect(page.getByRole('link', { name: new RegExp(replacementAgent.name) })).toBeVisible()
      }
      release.resolve()
      await expect(page.getByRole('link', { name: new RegExp(delayedAgent.name) })).toHaveCount(0)
      await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toHaveCount(0)
    } finally { release.resolve() }
  })
}

test('a failed bounded existence probe never redirects to onboarding and can be retried', async ({ page }) => {
  await prepareWorkspace(page)
  let probes = 0
  let available = false
  await page.route('**/api/onboarding/status', (route) => route.fulfill({ json: { exists: false, status: 'not_started', current_step: '', entry_mode: null, personal_assistant_agent_id: null, completed_at: null } }))
  await page.route('**/api/agents/page?*', (route) => {
    const params = new URL(route.request().url()).searchParams
    expect(params.has('cursor')).toBe(false)
    if (params.get('limit') === '1') {
      probes++
      if (!available) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
    }
    return route.fulfill({ json: { items: [originalAgent], next_cursor: cursor, has_more: true } })
  })
  await page.goto('/app/agents')
  await expect(page.getByRole('button', { name: 'Retry onboarding check' })).toBeVisible()
  await expect(page).toHaveURL('/app/agents')
  const failedProbes = probes
  available = true
  await page.getByRole('button', { name: 'Retry onboarding check' }).click()
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()
  expect(probes).toBe(failedProbes + 1)
})

test('an initial directory failure shows retry instead of an empty directory', async ({ page }) => {
  await prepareWorkspace(page)
  let available = false
  await page.route('**/api/agents/page?*', (route) => {
    const probe = new URL(route.request().url()).searchParams.get('limit') === '1'
    return available || probe
      ? route.fulfill({ json: { items: [originalAgent], next_cursor: null, has_more: false } })
      : route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
  })
  await page.goto('/app/agents')
  await expect(page.getByRole('button', { name: 'Retry loading agents' })).toBeVisible()
  await expect(page.getByText('No agents yet.', { exact: false })).toHaveCount(0)
  available = true
  await page.getByRole('button', { name: 'Retry loading agents' }).click()
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()
})

for (const outcome of ['skip', 'complete'] as const) {
  test(`onboarding ${outcome} refreshes bounded existence without a false redirect`, async ({ page }) => {
    await prepareWorkspace(page)
    let completed = false
    const status = () => ({ exists: completed, status: completed ? 'completed' : 'not_started', current_step: '', entry_mode: null, personal_assistant_agent_id: completed ? delayedAgent.id : null, completed_at: null })
    const limits: string[] = []
    await page.route('**/api/onboarding/status', (route) => route.fulfill({ json: status() }))
    await page.route('**/api/agents/page?*', (route) => {
      const params = new URL(route.request().url()).searchParams
      expect(params.has('cursor')).toBe(false)
      limits.push(params.get('limit') ?? '')
      return route.fulfill({ json: { items: completed ? [delayedAgent] : [], next_cursor: null, has_more: false } })
    })
    await page.route('**/api/onboarding/start', (route) => route.fulfill({ json: status() }))
    await page.route('**/api/onboarding/personal-assistant', (route) => route.fulfill({ json: { agent: delayedAgent, onboarding: status(), start: { status: 'started' } } }))
    await page.route(`**/api/agents/${delayedAgent.id}/files/**`, (route) => route.fulfill({ json: [] }))
    await page.route('**/api/onboarding/complete', (route) => {
      completed = true
      return route.fulfill({ json: status() })
    })
    await page.goto('/app/agents')
    await expect(page).toHaveURL('/app/onboarding')
    await expect(page.getByRole('button', { name: 'Create assistant', exact: true })).toBeVisible()
    const before = limits.filter((limit) => limit === '1').length
    if (outcome === 'skip') {
      await page.getByRole('button', { name: 'Skip for now' }).click()
      await expect(page).toHaveURL('/app/agents')
      await expect(page.getByText('No agents yet.', { exact: false })).toBeVisible()
    } else {
      await page.getByRole('button', { name: 'Create assistant', exact: true }).click()
      await expect(page).toHaveURL(`/app/agents/${delayedAgent.id}/chat`)
      expect(limits.filter((limit) => limit === '1').length).toBeGreaterThan(before)
      await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
      await expect(page.getByRole('link', { name: new RegExp(delayedAgent.name) })).toBeVisible()
    }
  })
}
