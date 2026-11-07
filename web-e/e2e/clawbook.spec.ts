import { expect, test } from '@playwright/test'

import { feedPost, linkedPost, prepareClawbook } from './clawbook-fixture'
import { originalUser } from './session-fencing-fixture'

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

test('feed loads older posts by cursor and a new post resets to the first page', async ({ page }, testInfo) => {
  await prepareClawbook(page)
  const cursor = 'older/+?='
  const olderPost = { ...feedPost, id: '0b8a3a53-6e4f-4a0e-9d44-3f1b3e4e8d11', content: 'Older company update' }
  const published = { ...feedPost, id: '7c9e6679-7425-40de-944b-e07fc1f90ae7', content: 'My session post', author_name: originalUser.display_name }
  const cursors: (string | null)[] = []
  let didPublish = false
  await page.route((url) => url.pathname === '/api/clawbook/posts/page', async (route) => {
    const position = new URL(route.request().url()).searchParams.get('cursor')
    cursors.push(position)
    const first = didPublish ? [published, feedPost] : [feedPost]
    await route.fulfill({ json: position === cursor
      ? { items: [feedPost, olderPost], next_cursor: null }
      : { items: first, next_cursor: cursor } })
  })
  await page.route('**/api/clawbook/posts', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    didPublish = true
    await route.fulfill({ json: published })
  })
  await page.goto('/app/clawbook')

  await expect(page.getByText('Latest company update', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Load more posts', exact: true }).click()
  await expect(page.getByText('Older company update', { exact: true })).toBeVisible()
  await expect(page.getByText('Latest company update', { exact: true })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Load more posts', exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('older-posts.png'), fullPage: true })

  await page.getByRole('textbox', { name: 'Share an update' }).fill('My session post')
  await page.getByRole('button', { name: 'Post', exact: true }).click()
  await expect(page.getByText('My session post', { exact: true })).toBeVisible()
  await expect(page.getByText('Older company update', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Load more posts', exact: true })).toBeEnabled()
  expect(cursors).toEqual([null, cursor, null])
  await page.screenshot({ path: testInfo.outputPath('after-post.png'), fullPage: true })
})

test('thread loads more comments through the post-scoped cursor', async ({ page }, testInfo) => {
  await prepareClawbook(page)
  const cursor = 'next/+?='
  const reply = (id: string, content: string) => ({ ...linkedPost.comments[0], id, post_id: feedPost.id, content })
  const cursors: (string | null)[] = []
  await page.route((url) => url.pathname === `/api/clawbook/posts/${feedPost.id}/comments/page`, async (route) => {
    const position = new URL(route.request().url()).searchParams.get('cursor')
    cursors.push(position)
    await route.fulfill({ json: position === cursor
      ? { items: [reply('c1f0a6f2-2f3d-4a52-8d0c-0a7c2f0d9b11', 'Second page reply')], next_cursor: null }
      : { items: [reply('5d8e4b0a-5c1e-4f7e-9b52-6a7a1f2f3c22', 'First page reply')], next_cursor: cursor } })
  })
  await page.goto('/app/clawbook')

  await page.getByRole('button', { name: 'Comments 0', exact: true }).click()
  await expect(page.getByText('First page reply', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Load more comments', exact: true }).click()
  await expect(page.getByText('Second page reply', { exact: true })).toBeVisible()
  await expect(page.getByText('First page reply', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Load more comments', exact: true })).toHaveCount(0)
  expect(cursors).toEqual([null, cursor])
  await page.screenshot({ path: testInfo.outputPath('more-comments.png'), fullPage: true })
})
