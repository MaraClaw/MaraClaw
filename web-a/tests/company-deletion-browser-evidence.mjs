import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export async function assertReducedPress(page, button, capture) {
  await button.scrollIntoViewIfNeeded()
  const bounds = await button.boundingBox()
  assert.ok(bounds)
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
  await page.mouse.down()
  try {
    assert.deepEqual(await button.evaluate((element) => {
      const style = getComputedStyle(element)
      return { scale: style.scale, transition: style.transitionProperty }
    }), { scale: '1', transition: 'none' })
    await page.screenshot({ path: capture, animations: 'disabled' })
  } finally {
    await page.mouse.move(1, 1)
    await page.mouse.up()
  }
}

export async function captureCompanyDeletionEvidence(page, browser, output) {
  const submit = page.getByRole('button', { name: 'Delete company permanently' })
  await page.screenshot({ path: join(output, 'company-preview-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await submit.scrollIntoViewIfNeeded()
  await page.screenshot({ path: join(output, 'company-preview-mobile.png'), fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true)
  await page.setViewportSize({ width: 1365, height: 1100 })
  if (!process.env.COMPANY_PREVIEW_BROWSER_SKILL) return

  const cdp = await browser.newBrowserCDPSession()
  const contrasts = []
  let skillBrowser
  try {
    const { arguments: args } = await cdp.send('Browser.getBrowserCommandLine')
    const profile = args.find((arg) => arg.startsWith('--user-data-dir=')).slice('--user-data-dir='.length)
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    const { loadOmowright } = await import(`${process.env.COMPANY_PREVIEW_BROWSER_SKILL}/scripts/omowright.mjs`)
    const { omowright } = await loadOmowright()
    skillBrowser = await omowright.connect(`http://127.0.0.1:${port}`)
    const target = (await skillBrowser.listTargets()).find((tab) => tab.url?.includes('/companies/alpha'))
    const ownedPage = await skillBrowser.attachPage(target.id)
    console.log(JSON.stringify(omowright.compactSnapshot(await ownedPage.snapshot())))
    const sid = await ownedPage.resolveSessionId()
    for (const device of ['iphone-14', 'desktop-1440', 'desktop-1920']) {
      await omowright.emulate(ownedPage, device === 'desktop-1920' ? { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false, hasTouch: false } : device)
      for (const scheme of ['light', 'dark']) {
        await ownedPage.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: scheme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, sid)
        await page.waitForFunction((value) => document.documentElement.dataset.theme === value, scheme)
        await page.waitForFunction(() => document.getAnimations().every((animation) => animation.effect?.getComputedTiming().iterations === Infinity || animation.playState === 'finished'))
        await page.getByRole('heading', { name: 'Alpha Labs' }).scrollIntoViewIfNeeded()
        await writeFile(join(output, `company-preview-${device}-${scheme}-top.png`), await ownedPage.screenshot())
        await submit.scrollIntoViewIfNeeded()
        await page.mouse.move(1, 1)
        const contrast = await submit.evaluate((element) => {
          const context = document.createElement('canvas').getContext('2d')
          if (!context) throw new Error('Canvas color conversion unavailable')
          const luminance = (color) => {
            context.fillStyle = color
            context.fillRect(0, 0, 1, 1)
            const [red, green, blue] = context.getImageData(0, 0, 1, 1).data
            const [linearRed, linearGreen, linearBlue] = [red, green, blue].map((channel) => {
              const value = channel / 255
              return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
            })
            return 0.2126 * linearRed + 0.7152 * linearGreen + 0.0722 * linearBlue
          }
          const contrastRatio = (target) => {
            const style = getComputedStyle(target)
            let background = target
            while (getComputedStyle(background).backgroundColor === 'rgba(0, 0, 0, 0)' && background.parentElement) background = background.parentElement
            const values = [luminance(style.color), luminance(getComputedStyle(background).backgroundColor)]
            return (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05)
          }
          const section = element.closest('[data-slot="card"]')
          return {
            destructiveTextContrast: contrastRatio(element),
            text: [...section.querySelectorAll('h3, p, dt, dd, label, input')].map((target) => ({
              element: target.tagName.toLowerCase(), fontSize: getComputedStyle(target).fontSize, contrast: contrastRatio(target),
            })),
          }
        })
        assert.ok(contrast.destructiveTextContrast >= 4.5, `${device} ${scheme} destructive text contrast: ${contrast.destructiveTextContrast}`)
        for (const text of contrast.text) assert.ok(text.contrast >= 4.5, `${device} ${scheme} ${text.element} text contrast: ${text.contrast}`)
        contrasts.push({ device, scheme, ...contrast })
        await writeFile(join(output, `company-preview-${device}-${scheme}-end.png`), await ownedPage.screenshot())
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true)
      }
    }
    await omowright.emulate(ownedPage, null)
    await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' })
    await writeFile(join(output, 'contrast.json'), JSON.stringify(contrasts, null, 2))
  } finally {
    if (skillBrowser) await skillBrowser.close()
    await cdp.detach()
  }
}

export async function captureCompanyNameStress(page, output) {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: 'Preview deletion' }).click()
  const input = page.getByRole('textbox', { name: /^Enter CompanyNameWithoutBreaks/ })
  await input.waitFor({ state: 'visible' })
  const section = page.locator('[data-slot="card"]').filter({ has: page.getByRole('heading', { name: 'Delete company', exact: true }) })
  for (const zoom of [1, 2]) {
    if (zoom === 2) await section.evaluate((element) => {
      for (const text of element.querySelectorAll('h3, p, dt, dd, label, input, button')) {
        const style = getComputedStyle(text)
        const fontSize = Number.parseFloat(style.fontSize)
        const lineHeight = Number.parseFloat(style.lineHeight)
        text.style.fontSize = `${fontSize * 2}px`
        if (Number.isFinite(lineHeight)) text.style.lineHeight = `${lineHeight * 2}px`
      }
    })
    await input.scrollIntoViewIfNeeded()
    assert.equal(await section.evaluate((element) => element.scrollWidth <= element.clientWidth), true, `Deletion section overflows at ${zoom * 100}% zoom`)
    await page.screenshot({ path: join(output, `company-preview-long-name-${zoom * 100}.png`), animations: 'disabled' })
  }
}
