// Owned-engine lifecycle shared only by the standalone admission browser scripts.
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'
import { bounded, listPath, makeFixture, responseBarrier } from './admission-fixture.browser.mjs'

export const exact = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&')}$`, 'u')
export const button = (page, name) => page.locator('button:not([aria-label])').filter({ hasText: exact(name) }).last()
export const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15_000 })
export const detached = (locator) => locator.waitFor({ state: 'detached', timeout: 15_000 })
export const enabledButton = (page, name) => page.locator('button:not([aria-label]):not(:disabled)').filter({ hasText: exact(name) }).last()
export const matrix = [390, 1440, 1920].flatMap((width) => ['light', 'dark'].map((theme) => ({ width, theme })))
const csp = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'none'"

export async function runBrowser(exercise) {
  assert.ok(process.env.ADMISSIONS_ARTIFACTS, 'Set ADMISSIONS_ARTIFACTS to a persistent evidence directory outside disposable TMPDIR')
  const output = resolve(process.env.ADMISSIONS_ARTIFACTS)
  await access(dirname(output)); await mkdir(output, { recursive: true })
  const receipt = {
    status: 'running', startedAt: new Date().toISOString(), output, engine: 'omowright.connectPipe',
    disclosure: 'All identities, organizations, requests and decisions are synthetic. Real Vite components; no backend.',
    matrix, scenarios: [], assertions: [], screenshots: [], requests: [], navigation: [], violations: [], blockedAssets: [],
    transport: { proxyDisabled: false, api: 'owned Vite middleware before proxy', cancelledResponses: 0 },
    cleanup: { barriersCreated: 0, barriersReleased: 0, tabsCreated: 0, tabsClosed: 0 },
  }
  const tabs = new Set(), states = new Set()
  let server, browser, omowright, work, active, closing, browserShutdown, stopped = false
  const checkInterrupted = () => { if (stopped) throw new Error('Interrupted during owned-resource startup') }
  const closeBrowser = () => { if (browser) browserShutdown ??= browser.close(); return browserShutdown }
  const verify = (name, actual, expected = true) => {
    assert.deepEqual(actual, expected, name); receipt.assertions.push({ scenario: active?.name ?? 'lifecycle', name })
  }
  const persist = async () => {
    await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
    const links = receipt.screenshots.map((shot) => `- [${shot.scenario}: ${shot.state} ${shot.edge}](${shot.file}) — request receipt through #${shot.requestsThrough}`)
    await writeFile(join(output, 'index.md'), [`# Synthetic admission browser QA`, `Status: ${receipt.status}`, '[HTTP/assertion/cleanup receipt](receipt.json)', ...links, ''].join('\n\n'))
    await writeFile(join(output, 'matrix-manifest.json'), JSON.stringify({ status: receipt.status, planned: matrix, captures: receipt.screenshots }, null, 2))
  }
  const cleanup = () => {
    closing ??= (async () => {
      const attempt = async (name, action) => {
        try { await action(); receipt.cleanup[name] = true }
        catch (error) { receipt.cleanup[name] = String(error); receipt.status = 'failed'; process.exitCode = 1 }
      }
      for (const state of states) for (const barrier of state.barriers) barrier.release()
      await attempt('remainingTabsClosed', async () => {
        const errors = []
        for (const tab of tabs) { try { await tab.close(); tabs.delete(tab); receipt.cleanup.tabsClosed++ } catch (error) { errors.push(error) } }
        if (errors.length) throw new AggregateError(errors)
      })
      await attempt('browserClosed', async () => { await closeBrowser() })
      await attempt('apiDrained', async () => { await bounded(Promise.all([...states].flatMap((state) => [...state.inFlight])), 'API cleanup did not drain') })
      await attempt('viteClosed', async () => { if (server) await server.close() })
      await attempt('profileAndViteCacheRemoved', async () => { if (work) await rm(work, { recursive: true, force: true }) })
      receipt.finishedAt = new Date().toISOString(); await persist()
    })()
    return closing
  }
  const interrupted = () => {
    stopped = true; receipt.status = 'failed'; receipt.error = 'Interrupted; owned cleanup requested'; process.exitCode = 130
    for (const state of states) for (const barrier of state.barriers) barrier.release()
    // Interrupt active commands, but let startup settle before final cleanup.
    // Otherwise connectPipe could finish after cleanup already removed its profile.
    void Promise.resolve().then(closeBrowser).catch((error) => { receipt.cleanup.signalBrowserClose = String(error) })
  }
  process.once('SIGINT', interrupted); process.once('SIGTERM', interrupted)
  async function capture(page, state, label) {
    await page.snapshot()
    for (const edge of ['top', 'bottom']) {
      await page.evaluate(async (end) => {
        for (const main of document.querySelectorAll('main')) main.scrollTop = end ? main.scrollHeight : 0
        window.scrollTo(0, end ? document.documentElement.scrollHeight : 0)
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
      }, edge === 'bottom')
      const dimensions = await page.evaluate(() => ({
        width: innerWidth, height: innerHeight, scale: devicePixelRatio, touch: navigator.maxTouchPoints, ua: navigator.userAgent,
        dark: document.documentElement.classList.contains('dark'), documentFits: document.documentElement.scrollWidth <= innerWidth,
        mainFits: [...document.querySelectorAll('main')].every((main) => main.scrollWidth <= main.clientWidth),
        scroll: [...document.querySelectorAll('main')].map((main) => ({ top: main.scrollTop, height: main.scrollHeight, client: main.clientHeight })),
      }))
      const file = `${state.name}-${label}-${edge}.png`
      await writeFile(join(output, file), await page.screenshot())
      receipt.screenshots.push({ scenario: state.name, state: label, edge, file, path: join(output, file), requestsThrough: receipt.requests.length, ...dimensions })
      verify('viewport width is exact', dimensions.width, state.width); verify('theme is active', dimensions.dark, state.theme === 'dark')
      verify('document fits horizontally', dimensions.documentFits); verify('main fits horizontally', dimensions.mainFits)
      if (state.width === 390) {
        verify('true mobile touch input', dimensions.touch > 0); verify('true mobile iPhone UA', /iPhone/u.test(dimensions.ua)); verify('mobile DPR', dimensions.scale, 3)
      }
    }
    await persist()
  }
  async function scenario(options, run) {
    const state = Object.assign(makeFixture(receipt, `admissions-${options.width}-${options.theme}`), options)
    states.add(state); active = state
    if (options.loading) state.loadingBarrier = responseBarrier(state, (call) => call.path === listPath)
    const viewport = { width: state.width, height: state.width === 390 ? 844 : 1000 }
    const manager = omowright.createAgentTabs(browser), tab = await manager.create('about:blank', { viewport })
    tabs.add(tab); receipt.cleanup.tabsCreated++
    const page = tab.page, errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    try {
      console.log(`START ${state.name}`)
      await omowright.emulate(page, state.width === 390 ? 'iphone-14' : { ...viewport, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      const session = await page.resolveSessionId()
      await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: state.theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
      await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `window.__admissionCsp=[];document.addEventListener('securitypolicyviolation',e=>window.__admissionCsp.push({uri:e.blockedURI,directive:e.effectiveDirective,disposition:e.disposition}));localStorage.setItem('maraclaw-admin-token','synthetic-admission-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(state.theme)});` }, session)
      await page.goto(`${server.resolvedUrls.local[0]}admission-approvals`)
      await manager.repin(tab.targetId, viewport)
      if (state.width === 390) await omowright.emulate(page, 'iphone-14')
      await visible(page.locator('main h1').filter({ hasText: exact('Admission approvals') }))
      await run(page, state)
      for (const event of await page.evaluate(() => window.__admissionCsp)) {
        if (event.disposition === 'enforce' && event.directive === 'style-src-elem' && event.uri.startsWith('https://fonts.googleapis.com/')) receipt.blockedAssets.push({ scenario: state.name, ...event })
        else receipt.violations.push({ scenario: state.name, message: 'Unexpected CSP boundary attempt', ...event })
      }
      verify('no uncaught page errors', errors, []); verify('synthetic API and CSP boundaries held', receipt.violations, [])
      receipt.scenarios.push({ name: state.name, status: 'passed' }); console.log(`PASS ${state.name}`)
    } catch (error) {
      receipt.failure = { scenario: state.name, message: String(error) }
      receipt.failure.diagnostics = []
      try { receipt.failure.snapshot = omowright.compactSnapshot(await page.snapshot()) }
      catch (captureError) { receipt.failure.diagnostics.push({ operation: 'snapshot', message: String(captureError) }) }
      try { await writeFile(join(output, `${state.name}-failure.png`), await page.screenshot()) }
      catch (captureError) { receipt.failure.diagnostics.push({ operation: 'screenshot', message: String(captureError) }) }
      throw error
    } finally {
      for (const barrier of state.barriers) barrier.release()
      try { if (tabs.has(tab)) { await tab.close(); tabs.delete(tab); receipt.cleanup.tabsClosed++ } }
      finally { await bounded(Promise.all(state.inFlight), 'Scenario API did not drain'); active = undefined; await persist() }
    }
  }
  try {
    if (process.env.OMO_BROWSER_ENGINE !== undefined) {
      receipt.status = 'blocked'; throw new Error(`Owned browser refused: OMO_BROWSER_ENGINE=${process.env.OMO_BROWSER_ENGINE}. Do not unset or bypass this restriction.`)
    }
    const skill = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
    const { loadOmowright } = await import(pathToFileURL(join(skill, 'scripts/omowright.mjs')).href)
    const loaded = await loadOmowright(); omowright = loaded.omowright; checkInterrupted()
    const executable = process.env.CHROMIUM_EXECUTABLE ?? (await import('@playwright/test')).chromium.executablePath()
    await access(executable); checkInterrupted() // Path discovery only. Never launch through Playwright.
    await access(tmpdir()); work = await mkdtemp(join(tmpdir(), 'admission-owned-'))
    assert.ok(output !== work && !output.startsWith(`${work}/`), 'Evidence must outlive the disposable profile')
    const profile = join(work, 'profile'); await mkdir(profile)
    server = await createServer({
      root: resolve(dirname(fileURLToPath(import.meta.url)), '..'), envDir: false, cacheDir: join(work, 'vite'),
      define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
      server: { host: '127.0.0.1', port: 0, strictPort: false, headers: { 'Content-Security-Policy': csp } },
      plugins: [{ name: 'synthetic-admission-firewall', enforce: 'pre',
        configResolved(config) { config.server.proxy = {}; receipt.transport.proxyDisabled = true },
        configureServer(vite) { vite.middlewares.use((request, response, next) => {
          if (!request.url.startsWith('/api/')) return next()
          if (active) return active.middleware(request, response)
          receipt.violations.push({ message: 'API without an active synthetic scenario', path: request.url })
          response.writeHead(502); response.end('Synthetic scenario required')
        }) },
      }],
    })
    await server.listen(); checkInterrupted(); verify('real backend proxy disabled', receipt.transport.proxyDisabled)
    browser = await omowright.connectPipe({ browserPath: executable, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-sync', `--user-data-dir=${profile}`], storageRoot: profile, commandTimeoutMs: 12_000, dialogPolicy: { accept: false } })
    checkInterrupted()
    await exercise({ scenario, capture, verify, receipt })
    verify('all response barriers released', receipt.cleanup.barriersReleased, receipt.cleanup.barriersCreated)
    verify('all owned tabs closed', receipt.cleanup.tabsClosed, receipt.cleanup.tabsCreated)
    receipt.status = 'passed'
  } catch (error) {
    if (receipt.status !== 'blocked') receipt.status = 'failed'
    receipt.error = String(error); process.exitCode = 1
  } finally {
    await cleanup(); process.off('SIGINT', interrupted); process.off('SIGTERM', interrupted)
    console.log(JSON.stringify({ status: receipt.status, output, assertions: receipt.assertions.length, captures: receipt.screenshots.length, cleanup: receipt.cleanup, error: receipt.error }))
  }
  return receipt
}
