import { apiRequest } from '@/lib/http'

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

export type ClawbookPostDetail = ClawbookPost & { comments: ClawbookComment[] }

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
