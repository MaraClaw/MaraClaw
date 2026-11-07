import { expect, type Page } from '@playwright/test'

import type { ClawbookComment, ClawbookPost, ClawbookPostDetail, ClawbookStats } from '../src/lib/clawbook-api'
import { originalToken, originalUser, prepareWorkspace } from './session-fencing-fixture'

export const feedPost: ClawbookPost = {
  id: 'f96a8aa1-a825-4dc3-98b0-643ba657d4d5', author_id: originalUser.id,
  author_name: 'Feed member', author_type: 'human', content: 'Latest company update',
  likes_count: 0, comments_count: 0, created_at: '2030-01-01T00:00:00Z',
}
export const linkedPost: ClawbookPostDetail = {
  ...feedPost, id: 'a28e950f-b757-4938-8cb3-66a0185ab319', author_name: 'Earlier member',
  content: 'Earlier update opened from Inbox', comments_count: 1,
  comments: [{ id: 'a8e4247a-a5b5-4ba8-9dd5-944976ced2b0', post_id: 'a28e950f-b757-4938-8cb3-66a0185ab319',
    author_id: originalUser.id, author_type: 'human', author_name: 'Reply member', content: 'Earlier thread reply',
    created_at: '2030-01-01T01:00:00Z' }],
}

export async function prepareClawbook(page: Page, unavailable = false) {
  await prepareWorkspace(page)
  let likes = 0
  let replies = [...linkedPost.comments]
  await page.route('**/api/clawbook/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    if (request.method() === 'GET') {
      switch (url.pathname) {
        case '/api/clawbook/stats':
          await route.fulfill({ json: {
            total_posts: 247, total_comments: 18, today_posts: 4, top_contributors: [],
          } satisfies ClawbookStats })
          return
        case '/api/clawbook/posts':
          await route.fulfill({ json: [{ ...feedPost, likes_count: likes }] })
          return
        case `/api/clawbook/posts/${linkedPost.id}`:
          await route.fulfill(unavailable ? { status: 404, json: { detail: 'Post not found' } } : {
            json: { ...linkedPost, comments_count: replies.length, comments: replies },
          })
          return
        case `/api/clawbook/posts/${feedPost.id}`:
          await route.fulfill({ json: { ...feedPost, likes_count: likes, comments: [] } })
          return
      }
    }
    if (request.method() === 'POST' && url.pathname === `/api/clawbook/posts/${feedPost.id}/like`) {
      expect([...url.searchParams]).toEqual([])
      likes = 1 - likes
      await route.fulfill({ json: { liked: Boolean(likes) } })
      return
    }
    if (request.method() === 'POST' && url.pathname === `/api/clawbook/posts/${linkedPost.id}/comments`) {
      expect(request.postDataJSON()).toEqual({ content: 'My session reply' })
      const reply = { ...linkedPost.comments[0], id: '142d1986-9cf2-4391-960b-9748eb7eb732',
        post_id: linkedPost.id, author_id: originalUser.id, author_type: 'human', author_name: originalUser.display_name,
        content: 'My session reply', created_at: '2030-01-01T02:00:00Z' } satisfies ClawbookComment
      replies = [...replies, reply]
      await route.fulfill({ json: reply })
      return
    }
    if (request.method() === 'POST' && url.pathname === '/api/clawbook/posts') {
      expect(request.postDataJSON()).toEqual({ content: 'My session post' })
      await route.fulfill({ json: { ...feedPost, content: 'My session post', author_name: originalUser.display_name } })
      return
    }
    expect(`${request.method()} ${url.pathname}`, 'Unexpected Clawbook request').toBe('a supported Clawbook request')
  })
}
