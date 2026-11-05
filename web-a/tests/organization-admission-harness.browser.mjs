// Owned OmOWright browser and Vite lifecycle for the organization review scenarios.
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createServer } from 'vite'
import { bounded, makeFixture } from './organization-admission-fixture.browser.mjs'
export { button, enabledButton, exact, visible, detached } from './admission-harness.browser.mjs'

export async function runBrowser(exercise) {
  assert.ok(process.env.ORGANIZATION_ADMISSIONS_ARTIFACTS)
  assert.equal(process.env.OMO_BROWSER_ENGINE, undefined, 'Respect browser engine restrictions')
  const output = resolve(process.env.ORGANIZATION_ADMISSIONS_ARTIFACTS)
  await mkdir(output, { recursive: true }); await access(tmpdir())
  const work = await mkdtemp(join(tmpdir(), 'organization-review-'))
  const receipt = { status: 'running', mode: process.env.QA_MODE ?? 'current', engine: 'omowright.connectPipe',
    disclosure: 'Real UI and synthetic endpoints only; no engine, provisioning or email delivery exercised.',
    assertions: [], screenshots: [], requests: [], violations: [], cleanup: { barriersCreated: 0, barriersReleased: 0, tabsCreated: 0, tabsClosed: 0 } }
  const states = new Set(), tabs = new Set()
  let server, browser, active, omowright
  const persist = () => writeFile(join(output, `${receipt.mode}-receipt.json`), JSON.stringify(receipt, null, 2))
  const verify = (name, actual, expected = true) => { assert.deepEqual(actual, expected, name); receipt.assertions.push(name) }
  async function capture(page, state, label) {
    await page.snapshot()
    const quote = await page.evaluate(() => document.querySelector('main dl') !== null)
    if (quote && receipt.mode !== 'before') {
      const fields = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('main dl > div')].map((field) => [field.querySelector('dt').textContent, field.querySelector('dd').textContent])))
      verify(`${label}: stored quote storage remains visible`, fields['Managed S3 storage per agent'], '1 GiB')
      verify(`${label}: stored quote file size remains visible`, fields['Maximum managed S3 file size'], '25 MiB')
      const text = await page.evaluate(() => document.querySelector('main').innerText)
      verify(`${label}: managed S3 scope remains visible`, text.includes('managed S3 uploads/files only'))
      verify(`${label}: guest local-write exclusion remains visible`, text.includes('Direct guest workspace/local writes are excluded'))
      verify(`${label}: stricter tool limit caveat remains visible`, text.includes('Some upload tools have stricter limits'))
    }
    for (const edge of quote && receipt.mode !== 'before' ? ['top', 'storage', 'bottom'] : ['top', 'bottom']) {
      await page.evaluate(async (position) => {
        for (const main of document.querySelectorAll('main')) main.scrollTop = position === 'bottom' ? main.scrollHeight : 0
        window.scrollTo(0, position === 'bottom' ? document.documentElement.scrollHeight : 0)
        if (position === 'storage') [...document.querySelectorAll('main dt')].find((field) => field.textContent === 'Managed S3 storage per agent').parentElement.scrollIntoView({ block: 'center', behavior: 'instant' })
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
      }, edge)
      const dimensions = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, scale: devicePixelRatio,
        dark: document.documentElement.classList.contains('dark'), touch: navigator.maxTouchPoints,
        fits: document.documentElement.scrollWidth <= innerWidth && [...document.querySelectorAll('main')].every((el) => el.scrollWidth <= el.clientWidth),
        scrollers: [...document.querySelectorAll('*')].filter((el) => /auto|scroll/u.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight).map((el) => el.id || el.tagName) }))
      verify('viewport matches', dimensions.width, state.width); verify('theme matches', dimensions.dark, state.theme === 'dark'); verify('horizontal content fits', dimensions.fits)
      if (state.width === 390) { verify('mobile DPR', dimensions.scale, 3); verify('mobile touch', dimensions.touch > 0) }
      const file = `${label}-${state.width}-${state.theme}-${edge}-${receipt.mode}.png`
      await writeFile(join(output, file), await page.screenshot()); receipt.screenshots.push({ file, ...dimensions })
    }
    await persist()
  }
  async function scenario(view, run) {
    const state = Object.assign(makeFixture(receipt, `${view.width}-${view.theme}`), view)
    active = state; states.add(state)
    const viewport = { width: view.width, height: view.width === 390 ? 844 : 1000 }
    const manager = omowright.createAgentTabs(browser), tab = await manager.create('about:blank', { viewport })
    tabs.add(tab); receipt.cleanup.tabsCreated++
    const page = tab.page, errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    try {
      await omowright.emulate(page, view.width === 390 ? 'iphone-14' : { ...viewport, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      const session = await page.resolveSessionId()
      await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: view.theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
      await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-admin-token','synthetic-organization-review');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(view.theme)})` }, session)
      await page.goto(`${server.resolvedUrls.local[0]}organization-admissions`)
      await manager.repin(tab.targetId, viewport)
      if (view.width === 390) await omowright.emulate(page, 'iphone-14')
      await run(page, state); verify('no page errors', errors, [])
    } catch (error) {
      receipt.failure = { message: String(error), snapshot: omowright.compactSnapshot(await page.snapshot()) }
      await writeFile(join(output, 'failure.png'), await page.screenshot()); throw error
    } finally {
      for (const barrier of state.barriers) barrier.release()
      await tab.close(); tabs.delete(tab); receipt.cleanup.tabsClosed++
      await bounded(Promise.all(state.inFlight)); active = undefined
    }
  }
  try {
    const { loadOmowright } = await import(pathToFileURL(join(process.env.BROWSER_SKILL_ROOT, 'scripts/omowright.mjs')).href)
    ;({ omowright } = await loadOmowright())
    const executable = (await import('@playwright/test')).chromium.executablePath(); await access(executable)
    const profile = join(work, 'profile'); await mkdir(profile)
    server = await createServer({ root: process.cwd(), envDir: false, cacheDir: join(work, 'vite'),
      define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
      server: { host: '127.0.0.1', port: 0, watch: { ignored: [output + '/**', work + '/**'] }, headers: { 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' ws://127.0.0.1:*" } },
      plugins: [{ name: 'synthetic-organization-firewall', enforce: 'pre', configResolved(config) { config.server.proxy = {} },
        configureServer(vite) { vite.middlewares.use((request, response, next) => {
          if (!request.url.startsWith('/api/')) return next()
          if (active) return active.middleware(request, response)
          receipt.violations.push('API outside scenario'); response.writeHead(502); response.end()
        }) } }] })
    await server.listen()
    browser = await omowright.connectPipe({ browserPath: executable, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', `--user-data-dir=${profile}`], storageRoot: profile, commandTimeoutMs: 12_000, dialogPolicy: { accept: false } })
    await exercise({ scenario, capture, verify, receipt }); verify('synthetic firewall held', receipt.violations, [])
    receipt.status = 'passed'
  } catch (error) { receipt.status = 'failed'; receipt.error = String(error); process.exitCode = 1 }
  finally {
    for (const state of states) for (const barrier of state.barriers) barrier.release()
    for (const tab of tabs) { await tab.close(); receipt.cleanup.tabsClosed++ }
    if (browser) await browser.close(); receipt.cleanup.browserClosed = true
    await bounded(Promise.all([...states].flatMap((state) => [...state.inFlight]))); receipt.cleanup.apiDrained = true
    if (server) await server.close(); receipt.cleanup.viteClosed = true
    await rm(work, { recursive: true, force: true }); receipt.cleanup.profileAndCacheRemoved = true
    await persist(); console.log(JSON.stringify({ status: receipt.status, mode: receipt.mode, captures: receipt.screenshots.length, assertions: receipt.assertions.length, cleanup: receipt.cleanup, error: receipt.error }))
  }
}
