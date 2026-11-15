import { test as base, expect } from '@playwright/test'
import { startApp } from './app-server.ts'
import { installSyntheticApi, syntheticState, type Actor } from './synthetic-api.ts'

type Fixtures = { actor: Actor; api: ReturnType<typeof syntheticState> }

export const test = base.extend<Fixtures, { app: Awaited<ReturnType<typeof startApp>> }>({
  actor: ['platform_admin', { option: true }],
  app: [async ({ browserName }, use) => {
    expect(browserName).toBe('chromium')
    const app = await startApp()
    try { await use(app) }
    finally { await app.close(); expect(app.blocked).toEqual([]) }
  }, { scope: 'worker' }],
  baseURL: async ({ app }, use) => { await use(app.url) },
  api: [async ({ context, app, actor, colorScheme }, use) => {
    const state = syntheticState(actor, colorScheme === 'dark' ? 'dark' : 'light')
    const errors: string[] = []
    context.on('page', (page) => page.on('pageerror', (error) => errors.push(error.message)))
    await installSyntheticApi(context, app.url, state)
    try { await use(state) }
    finally {
      state.createGate.resolve()
      await context.unrouteAll({ behavior: 'wait' })
      expect(state.unexpected, 'Unexpected HTTP calls').toEqual([])
      expect(errors, 'Browser runtime errors').toEqual([])
    }
  }, { auto: true }],
})

export { expect }
