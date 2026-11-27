import { expect, test, type Page } from '@playwright/test'
import { LANGUAGE_STORAGE_KEY } from '../src/lib/i18n/translation'

const CATALOG_CHUNK = /\/assets\/catalog-[\w-]+\.js/
const ENGLISH_FAQ = 'What is MaraClaw?'
const VIETNAMESE_FAQ = 'MaraClaw là gì?'
const switchToVietnamese = (page: Page) => page.getByRole('button', { name: 'Language: English. Switch to Tiếng Việt' }).first()

async function openLanding(page: Page, stored?: 'vi') {
  const requested: string[] = []
  page.on('request', (request) => requested.push(new URL(request.url()).pathname))
  await page.route('**/api/organization-plans', (route) => route.fulfill({ status: 503, json: { detail: 'Unavailable' } }))
  if (stored) await page.addInitScript(([key, value]) => window.localStorage.setItem(key, value), [LANGUAGE_STORAGE_KEY, stored])
  return requested
}

test('an English visit never requests the Vietnamese catalog or the phone-number form', async ({ page }) => {
  const requested = await openLanding(page)
  await page.goto('/')
  await expect(page.getByText(ENGLISH_FAQ)).toBeVisible()
  expect(requested.filter((path) => CATALOG_CHUNK.test(path) || /organization-(admission-form|quote-storage)-/.test(path))).toEqual([])
})

test('a stored Vietnamese locale holds the first render until its strings load, with no English frame', async ({ page }) => {
  const requested = await openLanding(page, 'vi')
  await page.addInitScript((english) => {
    const seen: string[] = []
    Object.assign(window, { englishFrames: seen })
    new MutationObserver(() => {
      const text = document.body?.innerText ?? ''
      if (text.includes(english) || text.includes('Loading…')) seen.push(text)
    }).observe(document, { childList: true, subtree: true, characterData: true })
  }, ENGLISH_FAQ)
  await page.goto('/')
  await expect(page.getByText(VIETNAMESE_FAQ)).toBeVisible()
  expect(await page.evaluate(() => Reflect.get(window, 'englishFrames'))).toEqual([])
  expect(requested.some((path) => CATALOG_CHUNK.test(path))).toBe(true)
  await expect(page.locator('html')).toHaveAttribute('lang', 'vi')
})

test('switching to Vietnamese loads the catalog on demand, then renders and persists it', async ({ page }) => {
  const requested = await openLanding(page)
  await page.goto('/')
  await expect(page.getByText(ENGLISH_FAQ)).toBeVisible()
  expect(requested.some((path) => CATALOG_CHUNK.test(path))).toBe(false)
  await switchToVietnamese(page).click()
  await expect(page.getByText(VIETNAMESE_FAQ)).toBeVisible()
  expect(requested.some((path) => CATALOG_CHUNK.test(path))).toBe(true)
  expect(await page.evaluate((key) => window.localStorage.getItem(key), LANGUAGE_STORAGE_KEY)).toBe('vi')
})

test('a failed catalog load falls back to English, keeps the stored choice and leaves the selector usable', async ({ page }) => {
  await openLanding(page, 'vi')
  await page.route(CATALOG_CHUNK, (route) => route.abort())
  await page.goto('/')
  await expect(page.getByText(ENGLISH_FAQ)).toBeVisible()
  expect(await page.evaluate((key) => window.localStorage.getItem(key), LANGUAGE_STORAGE_KEY)).toBe('vi')
  await switchToVietnamese(page).click()
  await expect(page.getByText(ENGLISH_FAQ)).toBeVisible()
  await page.unroute(CATALOG_CHUNK)
  await switchToVietnamese(page).click()
  await expect(page.getByText(VIETNAMESE_FAQ)).toBeVisible()
})
