import { chromium, type Browser, type PlaywrightWorkerArgs } from '@playwright/test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

// Use a task-owned omowright process, with Playwright's CDP bridge for routed fixtures.
export async function ownedUploadBrowser(
  { playwright }: Pick<PlaywrightWorkerArgs, 'playwright'>,
  provide: (browser: Browser) => Promise<void>,
) {
    const root = process.env.BROWSER_SKILL_ROOT
    if (!root) throw new Error('BROWSER_SKILL_ROOT is required for owned upload QA')
    const { loadOmowright } = await import(pathToFileURL(join(root, 'scripts/omowright.mjs')).href)
    const { omowright } = await loadOmowright()
    const profile = await mkdtemp(join(tmpdir(), 'upload-owned-'))
    try {
      const owned = await omowright.connectPipe({
        browserPath: chromium.executablePath(), storageRoot: profile,
        browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`],
      })
      try {
        const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
        const browser = await playwright.chromium.connectOverCDP(`http://127.0.0.1:${port}`)
        try { await provide(browser) }
        finally { await browser.close() }
      } finally { await owned.close() }
    } finally {
      await rm(profile, { recursive: true, force: true })
      console.log(`Owned upload browser teardown: process closed; profile removed ${profile}`)
    }
}
