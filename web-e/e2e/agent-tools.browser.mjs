import assert from 'node:assert/strict'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = process.env.AGENT_TOOLS_ARTIFACTS
assert.ok(output, 'AGENT_TOOLS_ARTIFACTS must name an owned evidence directory')
assert.ok(process.env.BROWSER_SKILL_ROOT, 'BROWSER_SKILL_ROOT must name the browser skill')
assert.ok(process.env.CHROMIUM_EXECUTABLE, 'CHROMIUM_EXECUTABLE must name an existing browser')
await mkdir(output, { recursive: true })
const { loadOmowright } = await import(pathToFileURL(join(process.env.BROWSER_SKILL_ROOT, 'scripts/omowright.mjs')).href)
const { omowright } = await loadOmowright()
const receipt = { engine: 'omowright.connectPipe', status: 'running', screenshots: [], paths: [], violations: [], cleanup: { browserClosed: false, viteClosed: false, profileRemoved: false } }
const profile = await mkdtemp(join(tmpdir(), 'agent-tools-browser-'))
let browser, server, fixtures
let overrides
let pending = new Set()
const responseBody = (path) => {
  const { originalUser: user, originalCompany: company, originalAgent: agent } = fixtures.session
  const { tool, toolId } = fixtures.tools
  const routes = {
    '/api/member/auth/me': user, '/api/auth/my-tenants': [{ tenant_id: company.id, tenant_name: company.name, tenant_slug: company.slug, logo_url: null }],
    '/api/tenants/me': company, '/api/company-admissions/mine/page': { items: [], next_cursor: null },
    '/api/onboarding/status': { exists: true, status: 'completed', current_step: 'completed', entry_mode: null, personal_assistant_agent_id: null, completed_at: '2030-01-01T00:00:00Z' },
    '/api/notifications/unread-count': { unread_count: 0 }, '/api/agents/page': { items: [agent], next_cursor: null, has_more: false }, '/api/agents/': [agent],
    [`/api/agents/${agent.id}`]: agent, [`/api/agents/${agent.id}/metrics`]: { status: 'stopped', tasks: { total: 0, done: 0, pending: 0 } },
    [`/api/agents/${agent.id}/openclaw-update`]: { notice: null, run: null, required: false, can_upgrade: false },
    [`/api/agents/${agent.id}/setup`]: { setup_pending: false, state: null, failure_code: null, retryable: false },
    [`/api/agents/${agent.id}/lifecycle`]: fixtures.lifecycle.lifecycleReady,
    [`/api/tools/agents/${agent.id}/with-config`]: [{ ...tool, agent_config: overrides }],
    [`/api/tools/agents/${agent.id}/tool-config/${toolId}`]: { global_config: tool.global_config, agent_config: overrides, merged_config: { ...tool.global_config, ...overrides }, config_schema: tool.config_schema },
  }
  assert.ok(Object.hasOwn(routes, path), `Unexpected synthetic API ${path}`)
  return routes[path]
}
try {
  server = await createServer({ root, envDir: false, define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, cacheDir: join(output, 'vite-cache'), plugins: [{
    name: 'agent-tools-owned-firewall',
    configResolved(config) { config.server.proxy = {} },
    configureServer(vite) { vite.middlewares.use((request, response, next) => {
      if (!request.url?.startsWith('/api/')) return next()
      const work = (async () => {
        try {
          const path = new URL(request.url, 'http://synthetic.invalid').pathname
          receipt.paths.push(path)
          assert.equal(request.headers.authorization, `Bearer ${fixtures.session.originalToken}`)
          let body
          if (request.method === 'GET') body = responseBody(path)
          else {
            const chunks = []; for await (const chunk of request) chunks.push(chunk)
            const input = JSON.parse(Buffer.concat(chunks).toString('utf8'))
            if (path === '/api/tools/test-mcp') {
              assert.deepEqual(input.saved, { agent_id: fixtures.session.originalAgent.id, tool_id: fixtures.tools.toolId })
              assert.equal(JSON.stringify(input.operations).includes('****'), false)
              body = { ok: true, tools: [] }
            } else {
              assert.equal(request.method, 'PATCH')
              assert.equal(path, `/api/tools/agents/${fixtures.session.originalAgent.id}/tool-config/${fixtures.tools.toolId}`)
              assert.equal(JSON.stringify(input).includes('****'), false)
              overrides = { ...Object.fromEntries(Object.entries(overrides).filter(([key]) => !input.unset.includes(key))), ...input.set }
              body = { ok: true }
            }
          }
          if (!response.destroyed) { response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end(JSON.stringify(body)) }
        } catch (error) {
          receipt.violations.push(String(error))
          if (!response.destroyed) { response.writeHead(502); response.end('{}') }
        }
      })()
      pending.add(work); void work.finally(() => pending.delete(work))
    }) },
  }], server: { host: '127.0.0.1', port: 0, headers: { 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; frame-src 'none'" } } })
  fixtures = { session: await server.ssrLoadModule('/e2e/session-fencing-fixture.ts'), tools: await server.ssrLoadModule('/e2e/agent-tools-fixture.ts'), lifecycle: await server.ssrLoadModule('/e2e/agent-lifecycle-data.ts') }
  overrides = { ...fixtures.tools.tool.agent_config }
  await server.listen()
  const origin = new URL(server.resolvedUrls.local[0]).origin
  browser = await omowright.connectPipe({ browserPath: process.env.CHROMIUM_EXECUTABLE, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', `--user-data-dir=${profile}`], storageRoot: profile })
  const page = await browser.newTab('about:blank')
  const session = await page.resolveSessionId()
  await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-enduser-token',${JSON.stringify(fixtures.session.originalToken)});` }, session)
  for (const width of [390, 1440, 1920]) for (const theme of ['light', 'dark']) {
    overrides = { ...fixtures.tools.tool.agent_config }
    await omowright.emulate(page, width === 390 ? 'iphone-14' : { width, height: 1000, deviceScaleFactor: 1, mobile: false, hasTouch: false })
    await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
    await page.goto(`${origin}${fixtures.tools.toolsPath}`)
    await omowright.emulate(page, width === 390 ? 'iphone-14' : { width, height: 1000, deviceScaleFactor: 1, mobile: false, hasTouch: false })
    await page.evaluate((value) => { localStorage.setItem('maraclaw-theme', value); document.documentElement.classList.toggle('dark', value === 'dark') }, theme)
    await page.snapshot()
    const expand = page.locator('button').filter({ hasText: /Synthetic shared MCP connector/u })
    await expand.waitFor({ state: 'visible' })
    await expand.click()
    const region = page.locator('select[aria-label="Region operation"]')
    await region.waitFor({ state: 'visible' })
    const capture = async (state) => {
      const metrics = await page.evaluate(() => ({ width: innerWidth, fits: document.documentElement.scrollWidth <= innerWidth, scrollers: [...document.querySelectorAll('*')].filter((element) => /auto|scroll/u.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight).map((element) => element.tagName) }))
      assert.equal(metrics.width, width); assert.equal(metrics.fits, true)
      const path = join(output, `member-${width}-${theme}-${state}.png`)
      await writeFile(path, await page.screenshot()); receipt.screenshots.push({ path, state, theme, ...metrics })
    }
    await capture('inherited')
    await region.selectOption('set')
    await page.getByLabel('New Region', { exact: true }).fill('ap-south-1')
    await page.locator('button').filter({ hasText: /^Test saved \+ draft$/u }).click()
    await page.locator('[role=status]').filter({ hasText: /Connection test passed/u }).waitFor({ state: 'visible' })
    await page.locator('button').filter({ hasText: /^Save config$/u }).click()
    await page.locator('[role=status]').filter({ hasText: /Configuration saved/u }).waitFor({ state: 'visible' })
    assert.equal(overrides.api_key, '********'); assert.equal(overrides.region, 'ap-south-1')
    await region.selectOption('unset')
    await page.locator('button').filter({ hasText: /^Save config$/u }).click()
    await page.locator('p').filter({ hasText: /^Using an inherited value\.$/u }).waitFor({ state: 'visible' })
    assert.equal(Object.hasOwn(overrides, 'region'), false)
    await capture('removed-override')
    console.log(`PASS owned member ${width} ${theme}`)
  }
  assert.deepEqual(receipt.violations, [])
  receipt.status = 'passed'
} catch (error) { receipt.status = 'failed'; receipt.error = String(error); process.exitCode = 1 }
finally {
  await Promise.all(pending)
  for (const [name, cleanup] of [['browserClosed', async () => { if (browser) await browser.close() }], ['viteClosed', async () => { if (server) await server.close() }], ['profileRemoved', () => rm(profile, { recursive: true, force: true })]]) {
    try { await cleanup(); receipt.cleanup[name] = true } catch (error) { receipt.cleanup[name] = String(error); process.exitCode = 1 }
  }
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt))
}
