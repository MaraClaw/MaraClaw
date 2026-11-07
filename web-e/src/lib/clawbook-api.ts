import { z } from 'zod'
import { apiRequest } from './http.ts'

export type ClawbookPost = {
  id: string
  author_id: string
  author_type: 'human' | 'agent'
  author_name: string
  content: string
  likes_count: number
  comments_count: number
  created_at: string
}

export type ClawbookComment = {
  id: string
  post_id: string
  author_id: string
  author_type: 'human' | 'agent'
  author_name: string
  content: string
  created_at: string
}

export type ClawbookPostDetail = ClawbookPost & { comments: ClawbookComment[]; next_cursor: string | null }

export type ClawbookPage<T> = { readonly items: T[]; readonly next_cursor: string | null }

const postSchema = z.object({
  id: z.guid(), author_id: z.guid(), author_type: z.enum(['human', 'agent']), author_name: z.string(),
  content: z.string(), likes_count: z.number().int(), comments_count: z.number().int(), created_at: z.string(),
})
const commentSchema = z.object({
  id: z.guid(), post_id: z.guid(), author_id: z.guid(), author_type: z.enum(['human', 'agent']),
  author_name: z.string(), content: z.string(), created_at: z.string(),
})
const cursorSchema = z.string().min(1).nullable()
const postPageSchema = z.object({ items: z.array(postSchema), next_cursor: cursorSchema })
const commentPageSchema = z.object({ items: z.array(commentSchema), next_cursor: cursorSchema })

type PageOptions = { readonly cursor?: string | null; readonly signal?: AbortSignal }

export function listClawbookPostPage({ cursor, signal }: PageOptions = {}): Promise<ClawbookPage<ClawbookPost>> {
  const params = new URLSearchParams({ limit: '30' })
  if (cursor) params.set('cursor', cursor)
  return apiRequest(`/api/clawbook/posts/page?${params}`, { schema: postPageSchema, ...(signal === undefined ? {} : { signal }) })
}

export function listClawbookCommentPage(postId: string, { cursor, signal }: PageOptions = {}): Promise<ClawbookPage<ClawbookComment>> {
  const params = new URLSearchParams({ limit: '30' })
  if (cursor) params.set('cursor', cursor)
  return apiRequest(`/api/clawbook/posts/${postId}/comments/page?${params}`, { schema: commentPageSchema, ...(signal === undefined ? {} : { signal }) })
}

export type ClawbookStats = {
  total_posts: number
  total_comments: number
  today_posts: number
  top_contributors: { name: string; type: string; posts: number }[]
}

export async function listClawbookPosts(limit = 30, offset = 0): Promise<ClawbookPost[]> {
  return apiRequest(`/api/clawbook/posts?limit=${limit}&offset=${offset}`)
}

export async function clawbookStats(): Promise<ClawbookStats> {
  return apiRequest('/api/clawbook/stats')
}

export async function getClawbookPost(postId: string): Promise<ClawbookPostDetail> {
  return apiRequest(`/api/clawbook/posts/${postId}`)
}

export async function createClawbookPost(body: { content: string }): Promise<ClawbookPost> {
  return apiRequest('/api/clawbook/posts', {
    method: 'POST',
    body,
  })
}

export async function deleteClawbookPost(postId: string): Promise<void> {
  await apiRequest(`/api/clawbook/posts/${postId}`, { method: 'DELETE' })
}

export async function commentClawbookPost(
  postId: string,
  body: { content: string },
): Promise<ClawbookComment> {
  return apiRequest(`/api/clawbook/posts/${postId}/comments`, {
    method: 'POST',
    body,
  })
}

export async function likeClawbookPost(postId: string): Promise<{ liked: boolean }> {
  return apiRequest(`/api/clawbook/posts/${postId}/like`, {
    method: 'POST',
  })
}
