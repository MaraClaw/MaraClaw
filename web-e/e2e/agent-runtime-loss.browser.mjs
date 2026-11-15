import assert from 'node:assert/strict'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = process.env.RUNTIME_LOSS_ARTIFACTS
const stage = process.env.RUNTIME_LOSS_STAGE
assert.ok(output && ['before', 'after'].includes(stage), 'Set RUNTIME_LOSS_ARTIFACTS and RUNTIME_LOSS_STAGE')
assert.ok(process.env.BROWSER_SKILL_ROOT && process.env.CHROMIUM_EXECUTABLE, 'Set owned browser paths')
assert.ok(process.env.RUN_TMP, 'RUN_TMP must name the owned runtime cache directory')
await mkdir(output, { recursive: true })
const { loadOmowright } = await import(pathToFileURL(join(process.env.BROWSER_SKILL_ROOT, 'scripts/omowright.mjs')).href)
const { omowright } = await loadOmowright()
const receipt = { engine: 'omowright.connectPipe', stage, status: 'running', screenshots: [], actions: [], violations: [], cleanup: {} }
const profile = await mkdtemp(join(tmpdir(), 'runtime-loss-browser-'))
const pending = new Set()
let browser, server, fixtures, agent, startBarrier, agentRead
let startResponse = 'success'
const detectedAt = '2030-01-01T01:00:00Z'
const responseBody = (path) => {
  const { originalUser: user, originalCompany: company } = fixtures.session
  const loss = agent.runtime_loss
  const routes = {
    '/api/member/auth/me': user,
    '/api/auth/my-tenants': [{ tenant_id: company.id, tenant_name: company.name, tenant_slug: company.slug, logo_url: null }],
    '/api/tenants/me': company, '/api/company-admissions/mine/page': { items: [], next_cursor: null },
    '/api/onboarding/status': { exists: true, status: 'completed', current_step: 'completed', entry_mode: null, personal_assistant_agent_id: null, completed_at: '2030-01-01T00:00:00Z' },
    '/api/notifications/unread-count': { unread_count: 0 },
    '/api/agents/page': { items: [agent], next_cursor: null, has_more: false }, '/api/agents/': [agent],
    '/api/agent-deletion-requests': { items: [], next_cursor: null },
    [`/api/agents/${agent.id}`]: agent,
    [`/api/agents/${agent.id}/metrics`]: { status: agent.status, tasks: { total: 0, done: 0, pending: 0 } },
    [`/api/agents/${agent.id}/openclaw-update`]: { notice: null, run: null, required: false, can_upgrade: false },
    [`/api/agents/${agent.id}/setup`]: { setup_pending: false, state: null, failure_code: null, retryable: false },
    [`/api/agents/${agent.id}/lifecycle`]: { ...fixtures.lifecycle.lifecycleReady, runtime: { recorded_status: 'running', recorded_container_id: 'synthetic-owned-guest', loss }, availability: { ...fixtures.lifecycle.lifecycleReady.availability, blocking_codes: [], can_start: true } },
  }
  assert.ok(Object.hasOwn(routes, path), `Unexpected synthetic GET ${path}`)
  return routes[path]
}
try {
  server = await createServer({ root, envDir: false, define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, cacheDir: join(process.env.RUN_TMP, `vite-${stage}`), plugins: [{
    name: 'runtime-loss-owned-firewall',
    configResolved(config) { config.server.proxy = {} },
    configureServer(vite) { vite.middlewares.use((request, response, next) => {
      if (!request.url?.startsWith('/api/')) return next()
      const work = (async () => {
        try {
          const path = new URL(request.url, 'http://synthetic.invalid').pathname
          assert.equal(request.headers.authorization, `Bearer ${fixtures.session.originalToken}`)
          let body, status = 200
          if (request.method === 'GET') {
            body = responseBody(path)
            if (path === `/api/agents/${agent.id}`) { agentRead?.resolve(); agentRead = undefined }
          }
          else {
            assert.equal(request.method, 'POST')
            const endpoint = agent.runtime_loss.reason === 'container_stopped' ? 'start' : 'start-new-container'
            assert.equal(path, `/api/agents/${agent.id}/${endpoint}`)
            const chunks = []; for await (const chunk of request) chunks.push(chunk)
            assert.equal(Buffer.concat(chunks).length, 0)
            receipt.actions.push({ method: request.method, path })
            if (startBarrier) await startBarrier.promise
            if (startResponse === 'refusal') { status = 409; body = { detail: { code: 'guest_memory_capacity_exceeded', message: 'Synthetic engine detail must stay hidden' } } }
            else { agent = { ...agent, status: 'running', runtime_loss: null }; body = agent }
          }
          if (!response.destroyed) { response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end(JSON.stringify(body)) }
        } catch (error) {
          receipt.violations.push(String(error))
          if (!response.destroyed) { response.writeHead(502); response.end('{}') }
        }
      })()
      pending.add(work); void work.finally(() => pending.delete(work))
    }) },
  }], server: { host: '127.0.0.1', port: 0, headers: { 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; frame-src 'none'" } } })
  fixtures = { session: await server.ssrLoadModule('/e2e/session-fencing-fixture.ts'), lifecycle: await server.ssrLoadModule('/e2e/agent-lifecycle-data.ts') }
  await server.listen()
  const origin = new URL(server.resolvedUrls.local[0]).origin
  browser = await omowright.connectPipe({ browserPath: process.env.CHROMIUM_EXECUTABLE, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', `--user-data-dir=${profile}`], storageRoot: profile })
  const page = await browser.newTab('about:blank')
  const session = await page.resolveSessionId()
  await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-enduser-token',${JSON.stringify(fixtures.session.originalToken)});` }, session)
  for (const width of [390, 1440, 1920]) for (const theme of ['light', 'dark']) {
    await omowright.emulate(page, width === 390 ? 'iphone-14' : { width, height: 1000, deviceScaleFactor: 1, mobile: false, hasTouch: false })
    await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
    const visit = async (reason, reader = false) => {
      agent = { ...fixtures.session.originalAgent, status: 'running', runtime_loss: { reason, detected_at: detectedAt }, ...(reader ? { access_level: 'use', creator_id: '11111111-1111-4111-8111-111111111111' } : {}) }
      await page.goto(`${origin}/app/agents/${agent.id}/settings`)
      await omowright.emulate(page, width === 390 ? 'iphone-14' : { width, height: 1000, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      await page.evaluate((value) => { localStorage.setItem('maraclaw-theme', value); document.documentElement.classList.toggle('dark', value === 'dark') }, theme)
      await page.snapshot()
      await page.locator('[role=alertdialog]').waitFor({ state: 'visible' })
    }
    const capture = async (state) => {
      const metrics = await page.evaluate(() => ({ width: innerWidth, fits: document.documentElement.scrollWidth <= innerWidth, scrollers: [...document.querySelectorAll('*')].filter((element) => /auto|scroll/u.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight).map((element) => element.tagName) }))
      assert.equal(metrics.width, width); assert.equal(metrics.fits, true)
      const path = join(output, `${state}-${width}-${theme}-${stage}.png`)
      await writeFile(path, await page.screenshot()); receipt.screenshots.push({ path, state, theme, ...metrics })
    }
    const dialog = page.locator('[role=alertdialog]')
    await visit('container_missing')
    await dialog.locator('button').filter({ hasText: /^Start a new container$/u }).waitFor({ state: 'visible' })
    await capture('missing-modal')
    await dialog.locator('button').filter({ hasText: /^Not now$/u }).click()
    await dialog.waitFor({ state: 'hidden' }); await capture('missing-inline')
    if (stage === 'after') {
      for (const reason of ['container_missing', 'image_unavailable']) {
        await visit(reason)
        await dialog.locator('button').filter({ hasText: /^Start a new container$/u }).click()
        await dialog.waitFor({ state: 'hidden' })
      }
      await visit('container_stopped')
      await dialog.locator('button').filter({ hasText: /^Start$/u }).waitFor({ state: 'visible' })
      await capture('stopped-modal')
      await dialog.locator('button').filter({ hasText: /^Not now$/u }).click()
      await dialog.waitFor({ state: 'hidden' }); await capture('stopped-inline')
      const refreshed = Promise.withResolvers(); agentRead = refreshed
      await page.evaluate(() => {
        const now = Date.now.bind(Date); Date.now = () => now() + 31_000
        window.dispatchEvent(new Event('offline')); window.dispatchEvent(new Event('online'))
      })
      await refreshed.promise
      assert.equal(await dialog.count(), 0)
      agent = { ...agent, runtime_loss: { ...agent.runtime_loss, detected_at: '2030-01-01T02:00:00Z' } }
      await page.evaluate(() => {
        const now = Date.now.bind(Date); Date.now = () => now() + 31_000
        window.dispatchEvent(new Event('offline')); window.dispatchEvent(new Event('online'))
      })
      await dialog.waitFor({ state: 'visible' })
      await capture('stopped-new-detection')
      startResponse = 'refusal'; startBarrier = Promise.withResolvers()
      await dialog.locator('button').filter({ hasText: /^Start$/u }).click()
      await dialog.locator('button:disabled').filter({ hasText: /^Starting\.\.\.$/u }).waitFor({ state: 'visible' })
      await capture('stopped-pending')
      await page.keyboard.press('Escape')
      assert.equal(await dialog.isVisible(), true)
      startBarrier.resolve(); startBarrier = undefined
      await dialog.locator('[role=alert]').waitFor({ state: 'visible' })
      assert.equal((await dialog.textContent()).includes('Synthetic engine detail'), false)
      await capture('stopped-refusal')
      startResponse = 'success'
      await dialog.locator('button').filter({ hasText: /^Start$/u }).click()
      await dialog.waitFor({ state: 'hidden' }); await capture('stopped-started')
      await visit('container_stopped', true)
      assert.equal(await dialog.locator('button').count(), 1)
      await capture('stopped-reader')
      receipt.actions.push({ scenario: 'same detected_at stays dismissed; new detected_at reopens; stopped pending/refusal/success, Settings recovery and reader guidance', width, theme, result: 'passed' })
    }
    console.log(`PASS owned runtime loss ${stage} ${width} ${theme}`)
  }
  assert.deepEqual(receipt.violations, [])
  receipt.status = 'passed'
} catch (error) { receipt.status = 'failed'; receipt.error = error instanceof Error ? error.stack : String(error); process.exitCode = 1 }
finally {
  startBarrier?.resolve()
  await Promise.all(pending)
  for (const [name, cleanup] of [['browserClosed', async () => { if (browser) await browser.close() }], ['viteClosed', async () => { if (server) await server.close() }], ['profileRemoved', () => rm(profile, { recursive: true, force: true })]]) {
    try { await cleanup(); receipt.cleanup[name] = true } catch (error) { receipt.cleanup[name] = String(error); process.exitCode = 1 }
  }
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt))
}
