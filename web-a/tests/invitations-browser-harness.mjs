import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { chromium, expect } from '@playwright/test'
import { createServer } from 'vite'

export const admin = {
  id: 'operator', identity_id: 'identity', email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'org_admin', is_platform_admin: false, tenant_id: 'tenant-a', title: null,
  primary_mobile: null, registration_source: null, is_active: true, email_verified: true,
  must_change_password: false, is_genesis: true, created_at: '2026-10-20T00:00:00Z',
}
export const invitation = {
  id: '9f695f83-6d23-449d-8916-578978d996d1', code: 'SYNTHETIC-INVITATION-ONLY', max_uses: 1,
  used_count: 0, is_active: true, created_at: '2026-10-24T12:00:00Z', expires_at: '2026-11-07T12:00:00Z',
  invited_email: 'recipient@example.test', redeemed_at: null, status: 'active',
}

export async function createInvitationsHarness() {
  if (process.env.OMO_BROWSER_ENGINE) throw new Error('Invitations QA requires the explicitly permitted owned browser engine')
  const output = process.env.INVITATIONS_ARTIFACTS ?? join(process.env.TMPDIR, 'invitations-evidence')
  await mkdir(output, { recursive: true })
  const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.cache/opencode/packages/oh-my-openagent@latest/node_modules/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(join(skillRoot, 'scripts/omowright.mjs'))
  const { omowright } = await loadOmowright()
  const server = await createServer({
    envDir: false, cacheDir: join(process.env.TMPDIR, 'invitations-vite-cache'),
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    plugins: [{ name: 'invitations-isolated-api', configResolved(config) { config.server.proxy = {} } }],
    server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } },
  })
  await server.listen()
  const base = server.resolvedUrls.local[0]
  const passed = []
  const captures = []
  const exchanges = []
  const pages = new WeakMap()
  let closedBrowsers = 0
  let closedSkillConnections = 0

  async function scenario(options, exercise) {
    const browser = await chromium.launch({ headless: true, args: ['--enable-automation', '--remote-debugging-port=0'] })
    let skillBrowser
    const state = {
      user: { ...admin, ...options.user }, token: 'synthetic-token', items: options.items ?? [], total: options.total ?? options.items?.length ?? 0,
      listStatus: options.listStatus ?? 200, listGate: options.listGate ?? null, sendStatus: 200, sendGate: null, revokeStatus: 204,
      exchanges: [], lists: [], barriers: [],
    }
    const errors = []
    try {
      const phone = options.width === 390
      const context = await browser.newContext({ viewport: { width: options.width ?? 1440, height: phone ? 844 : 1080 }, deviceScaleFactor: phone ? 3 : 1, isMobile: phone, hasTouch: phone, colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
      await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-token'))
      const page = await context.newPage()
      page.on('pageerror', (error) => errors.push(error.message))
      page.on('console', (message) => {
        if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) errors.push(message.text())
      })
      await page.route('**/api/**', async (route) => {
        const request = route.request()
        const url = new URL(request.url())
        const path = url.pathname
        let body
        let status = 200
        let gate
        if (path === '/api/auth/me') body = state.user
        else if (path === '/api/auth/login') body = { access_token: state.token, token_type: 'bearer', user: state.user }
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/enterprise/transfer-review-access') body = { can_review: false }
        else if (path === '/api/enterprise/invitation-codes' && request.method() === 'GET') {
          state.lists.push(url.search)
          gate = state.listGate
          status = state.listStatus
          const number = Number(url.searchParams.get('page'))
          body = { items: state.items.slice((number - 1) * 20, number * 20), total: state.total, page: number, page_size: 20 }
        } else if (path === '/api/enterprise/invite-users') {
          gate = state.sendGate
          status = state.sendStatus
          state.exchanges.push({ method: request.method(), body: request.postDataJSON() })
          body = { invited: options.invited ?? request.postDataJSON().emails.length, message: 'Invitations queued' }
        } else if (path.startsWith('/api/enterprise/invitation-codes/') && request.method() === 'DELETE') {
          status = state.revokeStatus
          state.exchanges.push({ method: 'DELETE', path })
          if (status < 400) state.items = state.items.map((item) => path.endsWith(`/${item.id}`) ? { ...item, status: 'revoked', is_active: false } : item)
        } else if (path === '/api/tenants/me') body = { id: 'tenant-a', name: 'Synthetic Organization' }
        else {
          errors.push(`Unexpected synthetic API ${request.method()} ${path}`)
          await route.abort()
          return
        }
        if (gate) { state.barriers.push(gate); await gate.promise }
        if (path === '/api/enterprise/invite-users' && status < 400) {
          const payload = request.postDataJSON()
          const created = payload.emails.slice(0, body.invited).map((email, index) => ({ ...invitation, id: `synthetic-new-${state.total + index}`, code: `SYNTHETIC-NEW-ONLY-${state.total + index}`, invited_email: email }))
          state.items = [...created, ...state.items]
          state.total += created.length
        }
        await route.fulfill({ status, contentType: 'application/json', body: status === 204 ? '' : JSON.stringify(status < 400 ? body : { detail: 'Organization email is disabled. Configure SMTP in Settings.' }) }).catch((error) => {
          if (!String(error).includes('closed') && !String(error).includes('cancel')) throw error
        })
      })
      await page.goto(`${base}${options.path ?? 'invitations'}`)
      const cdp = await browser.newBrowserCDPSession()
      try {
        const { arguments: args } = await cdp.send('Browser.getBrowserCommandLine')
        const profile = args.find((arg) => arg.startsWith('--user-data-dir=')).slice('--user-data-dir='.length)
        const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
        skillBrowser = await omowright.connect(`http://127.0.0.1:${port}`)
        const target = (await skillBrowser.listTargets()).find((tab) => tab.url?.startsWith(base))
        assert.ok(target)
        pages.set(page, await skillBrowser.attachPage(target.id))
      } finally { await cdp.detach() }
      await exercise(page, state)
      assert.deepEqual(errors, [])
      exchanges.push({ scenario: options.name, requests: state.exchanges })
      passed.push(options.name)
      console.log(`PASS ${options.name}`)
    } finally {
      state.barriers.forEach((barrier) => { barrier.resolve() })
      state.sendGate?.resolve()
      state.listGate?.resolve()
      if (skillBrowser) { await skillBrowser.close(); closedSkillConnections++ }
      await browser.close(); closedBrowsers++
    }
  }

  async function capture(page, name) {
    await page.evaluate(async () => {
      await new Promise(requestAnimationFrame)
      await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity).map((animation) => animation.finished.catch((error) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) throw error
      })))
    })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    assert.equal(await page.locator('main').evaluate((element) => element.scrollWidth <= element.clientWidth), true)
    const ownedPage = pages.get(page)
    assert.ok(ownedPage)
    await writeFile(join(output, `${name}.png`), await ownedPage.screenshot())
    await writeFile(join(output, `${name}.json`), JSON.stringify(omowright.compactSnapshot(await ownedPage.snapshot()), null, 2))
    const probe = await page.evaluate(() => ({
      viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio }, theme: document.documentElement.dataset.theme,
      documentOverflow: document.documentElement.scrollWidth > innerWidth,
      scrollOwners: [...document.querySelectorAll('*')].filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight).map((element) => element.id ? `#${element.id}` : element.tagName.toLowerCase()),
      controls: [...document.querySelectorAll('main button, main input, main a')].map((element) => ({ tag: element.tagName, name: element.getAttribute('aria-label') ?? (element instanceof HTMLInputElement ? [...element.labels].map((label) => label.textContent).join(' ') : element.textContent), value: element instanceof HTMLInputElement ? element.value : undefined, invalid: element.getAttribute('aria-invalid'), border: getComputedStyle(element).borderColor, width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height })),
      badges: [...document.querySelectorAll('main [data-slot="badge"]')].map((element) => ({ text: element.textContent, color: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor })),
      focus: { tag: document.activeElement?.tagName, text: document.activeElement?.textContent?.slice(0, 80), outline: document.activeElement ? getComputedStyle(document.activeElement).outlineStyle : null },
    }))
    await writeFile(join(output, `${name}-probe.json`), JSON.stringify(probe, null, 2))
    captures.push(name)
  }

  async function close(completed) {
    await server.close()
    const receipt = { completed, passed, captures, exchanges, output, cleanup: { closedBrowsers, closedSkillConnections, viteClosed: true, syntheticAPIsOnly: true } }
    await writeFile(join(output, `${process.env.INVITATIONS_BASELINE ? 'baseline-' : ''}receipt.json`), JSON.stringify(receipt, null, 2))
    console.log(JSON.stringify(receipt))
  }
  return { base, output, scenario, capture, close }
}

export { expect }
