import { expect, test } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { ownedUploadBrowser } from './upload-policy-browser-fixture'
import { openUploads, prepareUploads } from './upload-policy-fixture'

if (process.env.BROWSER_SKILL_ROOT) test.use({ browser: ownedUploadBrowser })

for (const width of [390, 1440, 1920]) {
  for (const theme of ['light', 'dark'] as const) {
    for (const surface of ['chat', 'workspace'] as const) {
      test(`${surface} upload policy ${width} ${theme}`, async ({ page }, info) => {
        const evidence = process.env.UPLOAD_EVIDENCE ?? info.outputPath('screenshots')
        await mkdir(evidence, { recursive: true })
        const phase = process.env.UPLOAD_CAPTURE_PHASE ?? 'after'
        await page.setViewportSize({ width, height: 900 })
        await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
        await page.addInitScript((theme) => localStorage.setItem('maraclaw-theme', theme), theme)
        const chat = await prepareUploads(page)
        await openUploads(page, surface)
        if (surface === 'chat') await chat.socket()
        if (phase === 'after') await expect(page.locator('input[type="file"]')).toBeEnabled()
        await page.screenshot({ path: join(evidence, `${surface}-ready-${width}-${theme}-${phase}.png`) })
        if (phase === 'before') return
        const scrollers = await page.evaluate(() => [...document.querySelectorAll('*')]
          .filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight)
          .map((el) => ({ tag: el.tagName, className: el.className })))
        await writeFile(join(evidence, `${surface}-${width}-${theme}-scrollers.json`), JSON.stringify(scrollers))
        await page.route('**/api/chat/upload-policy', (route) => route.fulfill({ status: 503, json: { detail: 'Policy unavailable' } }))
        await page.reload()
        await expect(page.getByRole('button', { name: 'Retry upload limits' })).toBeVisible()
        await expect(page.locator('input[type="file"]')).toBeDisabled()
        await page.screenshot({ path: join(evidence, `${surface}-error-${width}-${theme}-${phase}.png`) })
      })
    }
  }
}
