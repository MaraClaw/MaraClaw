import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createServer } from 'vite'

import { bounded, paymentFixture, responseBarrier } from './transfer-payment-fixture.browser.mjs'

export const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15_000 })
export const button = (page, text) => page.locator('main button').filter({ hasText: new RegExp(`^${text}$`, 'u') }).last()
export const matrix = [390, 1440, 1920].flatMap((width) => ['light', 'dark'].map((theme) => ({ width, theme })))
const tempRoot = '/var/folders/fm/dhwbb9lj4yn1dkrn1qncq8sh0000gn/T/opencode'
const csp = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'none'"

export async function runPayments(exercise) {
  assert.equal(process.env.OMO_BROWSER_ENGINE, undefined, 'Owned browser requires an unset OMO_BROWSER_ENGINE; never bypass the restriction')
  await access(tempRoot)
  const work = await mkdtemp(join(tempRoot, 'payment-review-runtime-'))
  const output = await mkdtemp(join(tempRoot, 'payment-review-evidence-'))
  const receipt = { status: 'running', work, output, syntheticOnly: true, matrix, scenarios: [], assertions: [], captures: [], requests: [], violations: [], cleanup: { barriersCreated: 0, barriersReleased: 0 }, proxyDisabled: false }
  let active, server, browser, omowright, executable
  const tabs = new Set(), states = new Set()
  const verify = (name, value, expected = true) => { assert.deepEqual(value, expected, name); receipt.assertions.push({ scenario: active?.name, name }) }
  const persist = () => writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  async function capture(page, state, label) {
    await page.snapshot()
    const segments = await page.evaluate(() => Math.max(2, Math.ceil(document.querySelector('main').scrollHeight / document.querySelector('main').clientHeight)))
    for (let index = 0; index < segments; index++) {
      const edge = index === 0 ? 'top' : index === segments - 1 ? 'bottom' : `middle-${index}`
      await page.evaluate(async ({ index, last }) => {
        const main = document.querySelector('main')
        main.scrollTop = last ? main.scrollHeight : index * main.clientHeight
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
      }, { index, last: index === segments - 1 })
      const metrics = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, dpr: devicePixelRatio, touch: navigator.maxTouchPoints, ua: navigator.userAgent, dark: document.documentElement.classList.contains('dark'), scrollTop: document.querySelector('main').scrollTop, scrollHeight: document.querySelector('main').scrollHeight, scrollViewport: document.querySelector('main').clientHeight, fits: document.documentElement.scrollWidth <= innerWidth && document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth, overflow: [...document.querySelectorAll('main *')].filter((element) => element.getBoundingClientRect().right > innerWidth).map((element) => ({ tag: element.tagName, class: element.className, right: element.getBoundingClientRect().right, text: element.textContent.slice(0, 80) })) }))
      receipt.lastMetrics = metrics
      const image = Buffer.from(await page.screenshot()), path = join(output, `${state.name}-${label}-${edge}.png`)
      verify('PNG signature', image.subarray(0, 8).toString('hex'), '89504e470d0a1a0a')
      verify('capture pixel width', image.readUInt32BE(16), metrics.width * metrics.dpr)
      verify('capture pixel height', image.readUInt32BE(20), metrics.height * metrics.dpr)
      verify('viewport', metrics.width, state.width); verify('theme', metrics.dark, state.theme === 'dark'); verify('horizontal fit', metrics.fits)
      if (state.width === 390) { verify('mobile touch', metrics.touch > 0); verify('iPhone UA', /iPhone/u.test(metrics.ua)); verify('mobile DPR', metrics.dpr, 3) }
      await writeFile(path, image); receipt.captures.push({ scenario: state.name, label, edge, path, ...metrics })
    }
    await persist()
  }
  async function scenario(options, run) {
    if (process.env.BROWSER_SCENARIO && !options.name.includes(process.env.BROWSER_SCENARIO)) return
    const state = paymentFixture(receipt, { width: 1440, theme: 'light', ...options })
    states.add(state); active = state
    const profile = join(work, state.name); await mkdir(profile)
    browser = await omowright.connectPipe({ browserPath: executable, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-sync', `--user-data-dir=${profile}`], storageRoot: profile, commandTimeoutMs: 12_000, dialogPolicy: { accept: false } })
    const viewport = { width: state.width, height: state.width === 390 ? 844 : 1000 }
    const manager = omowright.createAgentTabs(browser), tab = await manager.create('about:blank', { viewport })
    tabs.add(tab)
    const page = tab.page, errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    try {
      console.log(`START ${state.name}`)
      await omowright.emulate(page, state.width === 390 ? 'iphone-14' : { ...viewport, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      const session = await page.resolveSessionId()
      await page.cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true }, session)
      await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: state.theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
      const pollClock = state.name === 'payment-390-light' ? `{
        const polls = new Map(), schedule = window.setInterval.bind(window), cancel = window.clearInterval.bind(window);
        window.setInterval = (callback, delay, ...args) => { const id = schedule(callback, delay, ...args); if (delay === 30000 && typeof callback === 'function') polls.set(id, () => callback(...args)); return id; };
        window.clearInterval = (id) => { polls.delete(id); cancel(id); };
        window.__runSyntheticPaymentPoll = () => { for (const callback of [...polls.values()]) callback(); };
      }` : ''
      await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-admin-token','synthetic-payment-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(state.theme)});${pollClock}` }, session)
      const go = async (path = '/transfer-payments') => {
        await page.goto(`${server.resolvedUrls.local[0]}${path.slice(1)}`)
        await manager.repin(tab.targetId, viewport)
        if (state.width === 390) await omowright.emulate(page, 'iphone-14')
      }
      await run({ page, state, go, hold: (match) => responseBarrier(state, match) })
      verify('no page errors', errors, []); verify('all APIs synthetic and recognized', receipt.violations, [])
      receipt.scenarios.push({ name: state.name, status: 'passed' }); console.log(`PASS ${state.name}`)
    } catch (error) {
      receipt.failure = { scenario: state.name, message: String(error) }
      try { receipt.failure.snapshot = omowright.compactSnapshot(await page.snapshot()); await writeFile(join(output, `${state.name}-failure.png`), await page.screenshot()) } catch (captureError) { receipt.failure.captureError = String(captureError) }
      throw error
    } finally {
      for (const barrier of state.barriers) barrier.release()
      await tab.close(); tabs.delete(tab); await browser.close(); browser = undefined
      await bounded(Promise.all(state.inFlight)); active = undefined; await persist()
    }
  }
  try {
    const skill = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
    const { loadOmowright } = await import(pathToFileURL(join(skill, 'scripts/omowright.mjs')).href)
    ;({ omowright } = await loadOmowright())
    executable = process.env.CHROMIUM_EXECUTABLE ?? (await import('@playwright/test')).chromium.executablePath()
    await access(executable)
    server = await createServer({
      root: resolve(dirname(new URL(import.meta.url).pathname), '..'), envDir: false, cacheDir: join(work, 'vite'),
      define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
      server: { host: '127.0.0.1', port: 0, strictPort: false, headers: { 'Content-Security-Policy': csp } },
      plugins: [{ name: 'synthetic-transfer-payments', enforce: 'pre',
        configResolved(config) { config.server.proxy = {}; receipt.proxyDisabled = true },
        configureServer(vite) { vite.middlewares.use((request, response, next) => {
          if (!request.url.startsWith('/api/')) return next()
          if (active) return active.middleware(request, response)
          receipt.violations.push({ message: 'API without synthetic scenario', path: request.url }); response.writeHead(502); response.end()
        }) },
      }],
    })
    await server.listen(); verify('backend proxy disabled', receipt.proxyDisabled)
    await exercise({ scenario, capture, verify, receipt })
    receipt.status = 'passed'
  } catch (error) { receipt.status = 'failed'; receipt.error = String(error); process.exitCode = 1 }
  finally {
    for (const state of states) for (const barrier of state.barriers) barrier.release()
    const attempt = async (name, action) => {
      try { await action(); receipt.cleanup[name] = true }
      catch (error) { receipt.cleanup[name] = String(error); receipt.status = 'failed'; process.exitCode = 1 }
    }
    await attempt('tabsClosed', async () => { for (const tab of tabs) await tab.close() })
    await attempt('browserClosed', async () => { if (browser) await browser.close() })
    await attempt('requestsDrained', async () => { await bounded(Promise.all([...states].flatMap((state) => [...state.inFlight]))) })
    await attempt('serverClosed', async () => { if (server) await server.close() })
    await attempt('runtimeRemoved', () => rm(work, { recursive: true, force: true }))
    receipt.finishedAt = new Date().toISOString(); await persist()
    console.log(JSON.stringify({ status: receipt.status, output, work, captures: receipt.captures.length, assertions: receipt.assertions.length, cleanup: receipt.cleanup, error: receipt.error }))
  }
}
