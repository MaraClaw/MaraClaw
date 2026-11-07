import { expect, test } from '@playwright/test'

import { feedPost, linkedPost, prepareClawbook } from './clawbook-fixture'

test('notification link loads a thread outside the feed and displays the server total', async ({ page }, testInfo) => {
  await prepareClawbook(page)

  await page.goto(`/app/clawbook?post=${linkedPost.id}`)

  await expect(page.getByText(/247 posts/)).toBeVisible()
  await expect(page.getByText(linkedPost.content, { exact: true })).toBeVisible()
  await expect(page.getByText('Earlier thread reply', { exact: true })).toBeVisible()
  await expect(page.getByText('Latest company update', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('linked-post.png'), fullPage: true })

  await page.getByRole('textbox', { name: 'Reply' }).fill('My session reply')
  await page.getByRole('button', { name: 'Reply', exact: true }).click()
  await expect(page.getByText('My session reply', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Like 0', exact: true }).last().click()
  await expect(page.getByRole('button', { name: 'Like 1', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: 'Share an update' }).fill('My session post')
  await page.getByRole('button', { name: 'Post', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Share an update' })).toHaveValue('')
})

test('unavailable notification link reports 404 and keeps the feed usable', async ({ page }, testInfo) => {
  await prepareClawbook(page, true)

  await page.goto(`/app/clawbook?post=${linkedPost.id}`)

  await expect(page.getByRole('status').filter({ hasText: 'This post is unavailable' })).toBeVisible()
  await expect(page.getByText('Latest company update', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Reply' })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('unavailable-post.png'), fullPage: true })
  await page.getByRole('button', { name: 'Comments 0', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Reply' })).toBeVisible()
})

test('post composer accepts and sends 1000 characters while blocking further typing', async ({ page }) => {
  await prepareClawbook(page)
  const content = 'a'.repeat(1000)
  await page.route('**/api/clawbook/posts', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    expect(route.request().postDataJSON()).toEqual({ content })
    await route.fulfill({ json: { ...feedPost, content } })
  })
  await page.goto('/app/clawbook')

  const draft = page.getByRole('textbox', { name: 'Share an update' })
  await draft.fill(content.slice(0, -1))
  await draft.press('a')
  await draft.press('b')

  await expect(draft).toHaveValue(content)
  await expect(page.getByText('1000/1000', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Post', exact: true }).click()
  await expect(page.getByText('Posted', { exact: true })).toBeVisible()
  await expect(draft).toHaveValue('')
})
