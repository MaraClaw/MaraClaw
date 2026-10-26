import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium } from '@playwright/test'

export async function launchDepartmentBrowser() {
  const root = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(pathToFileURL(join(root, 'scripts/omowright.mjs')).href)
  const { omowright } = await loadOmowright()
  const profile = await mkdtemp(join(tmpdir(), 'departments-owned-'))
  let owned
  let browser
  try {
    owned = await omowright.connectPipe({
      browserPath: process.env.CHROMIUM_EXECUTABLE ?? chromium.executablePath(),
      browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`],
      storageRoot: profile,
    })
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
    return {
      browser,
      async close() {
        try { await browser.close() }
        finally {
          try { await owned.close() }
          finally { await rm(profile, { recursive: true, force: true }) }
        }
      },
    }
  } catch (error) {
    try { await owned?.close() }
    finally { await rm(profile, { recursive: true, force: true }) }
    throw error
  }
}
