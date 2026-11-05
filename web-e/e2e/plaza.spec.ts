import { expect, test } from '@playwright/test'

import { linkedPost, preparePlaza } from './plaza-fixture'

test('notification link loads a thread outside the feed and displays the server total', async ({ page }, testInfo) => {
  await preparePlaza(page)

  await page.goto(`/app/plaza?post=${linkedPost.id}`)

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
  await preparePlaza(page, true)

  await page.goto(`/app/plaza?post=${linkedPost.id}`)

  await expect(page.getByRole('status').filter({ hasText: 'This post is unavailable' })).toBeVisible()
  await expect(page.getByText('Latest company update', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Reply' })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('unavailable-post.png'), fullPage: true })
  await page.getByRole('button', { name: 'Comments 0', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Reply' })).toBeVisible()
})
