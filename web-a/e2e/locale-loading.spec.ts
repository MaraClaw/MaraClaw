import { test, expect } from './fixtures.ts'

const DICTIONARY_MODULE = /\/src\/lib\/translations\.ts/
const STORAGE_KEY = 'maraclaw-admin-language'
const switchToVietnamese = (page: import('@playwright/test').Page) =>
  page.getByRole('button', { name: 'Language: English. Switch to Tiếng Việt' })

test.describe('anonymous', () => {
  test.use({ actor: 'anonymous' })

  test('an English login never requests the Vietnamese dictionary', async ({ page }) => {
    const requested: string[] = []
    page.on('request', (request) => requested.push(request.url()))
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
    expect(requested.filter((url) => DICTIONARY_MODULE.test(url))).toEqual([])
  })

  test('a stored Vietnamese locale holds the first render until its strings load', async ({ page }) => {
    await page.addInitScript((key) => window.localStorage.setItem(key, 'vi'), STORAGE_KEY)
    await page.addInitScript(() => {
      const seen: string[] = []
      Object.assign(window, { englishFrames: seen })
      new MutationObserver(() => {
        const text = document.body?.innerText ?? ''
        if (text.includes('Sign in')) seen.push(text)
      }).observe(document, { childList: true, subtree: true, characterData: true })
    })
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Đăng nhập', exact: true })).toBeVisible()
    expect(await page.evaluate(() => Reflect.get(window, 'englishFrames'))).toEqual([])
    await expect(page.locator('html')).toHaveAttribute('lang', 'vi')
  })

  test('switching to Vietnamese loads the dictionary on demand and persists the choice', async ({ page }) => {
    const requested: string[] = []
    page.on('request', (request) => requested.push(request.url()))
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
    expect(requested.some((url) => DICTIONARY_MODULE.test(url))).toBe(false)
    await switchToVietnamese(page).click()
    await expect(page.getByRole('button', { name: 'Đăng nhập', exact: true })).toBeVisible()
    expect(requested.some((url) => DICTIONARY_MODULE.test(url))).toBe(true)
    expect(await page.evaluate((key) => window.localStorage.getItem(key), STORAGE_KEY)).toBe('vi')
  })

  test('a failed dictionary load falls back to English, keeps the stored choice and leaves the toggle usable', async ({ page }) => {
    await page.addInitScript((key) => window.localStorage.setItem(key, 'vi'), STORAGE_KEY)
    await page.route(DICTIONARY_MODULE, (route) => route.abort())
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
    expect(await page.evaluate((key) => window.localStorage.getItem(key), STORAGE_KEY)).toBe('vi')
    await page.unroute(DICTIONARY_MODULE)
    await switchToVietnamese(page).click()
    await expect(page.getByRole('button', { name: 'Đăng nhập', exact: true })).toBeVisible()
  })
})
