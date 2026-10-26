import { expect, test } from '@playwright/test'
import { controlPage, prepareCapture, savedCredential } from './control-capture-fixture'

for (const width of [390, 1440, 1920]) {
  for (const theme of ['light', 'dark'] as const) {
    test(`capture selection at ${width}px in ${theme} mode`, async ({ page }, testInfo) => {
      // Given
      const fixture = await prepareCapture(page)
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
      await page.addInitScript((theme) => localStorage.setItem('maraclaw-theme', theme), theme)
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await page.goto(controlPage)
      await expect(page.getByRole('option', { name: /Team account/ })).toHaveCount(1)
      await page.getByRole('button', { name: 'Take control', exact: true }).click()
      await expect(page.getByLabel('Cookie domain on unlock')).toHaveValue('vendor.example.test')
      // When: native select remains keyboard-operable.
      const select = page.getByLabel('Save cookies on release')
      await expect(select).toBeEnabled()
      await select.focus()
      await page.keyboard.press('t')
      await page.keyboard.press('Enter')
      // Then
      await expect(select).toHaveValue(savedCredential.id)
      await expect(select).toBeFocused()
      await expect(page.getByRole('button', { name: 'Release control', exact: true })).toBeEnabled()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
      const screenshot = testInfo.outputPath(`after-${width}-${theme}.png`)
      await page.screenshot({ path: screenshot, fullPage: true })
      await testInfo.attach('capture-selection', { path: screenshot, contentType: 'image/png' })
      const scrollers = await page.evaluate(() => [...document.querySelectorAll('*')]
        .filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight)
        .map((element) => `${element.tagName.toLowerCase()}.${element.className}`))
      await testInfo.attach('scroll-owners', { body: JSON.stringify(scrollers), contentType: 'application/json' })
      await page.getByAltText('Live AgentBay session').scrollIntoViewIfNeeded()
      const scrollEnd = testInfo.outputPath(`after-${width}-${theme}-scroll-end.png`)
      await page.screenshot({ path: scrollEnd })
      await testInfo.attach('capture-scroll-end', { path: scrollEnd, contentType: 'image/png' })
      expect(fixture.errors).toEqual([])
    })
  }
}
