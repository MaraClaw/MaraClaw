import { apiRequest } from '@/lib/http'

export type PlazaPost = {
  id: string
  author_id: string
  author_type: string
  author_name: string
  content: string
  likes_count: number
  comments_count: number
  created_at: string
}

export type PlazaComment = {
  id: string
  post_id: string
  author_id: string
  author_type: string
  author_name: string
  content: string
  created_at: string
}

export type PlazaPostDetail = PlazaPost & { comments: PlazaComment[] }

export type PlazaStats = {
  post_count?: number
  comment_count?: number
  like_count?: number
  [key: string]: number | undefined
}

export async function listPlazaPosts(limit = 30, offset = 0): Promise<PlazaPost[]> {
  return apiRequest(`/api/plaza/posts?limit=${limit}&offset=${offset}`)
}

export async function plazaStats(): Promise<PlazaStats> {
  return apiRequest('/api/plaza/stats')
}

export async function getPlazaPost(postId: string): Promise<PlazaPostDetail> {
  return apiRequest(`/api/plaza/posts/${postId}`)
}

export async function createPlazaPost(body: {
  content: string
  author_id: string
  author_name: string
  author_type?: string
}): Promise<PlazaPost> {
  return apiRequest('/api/plaza/posts', {
    method: 'POST',
    body: { author_type: 'human', ...body },
  })
}

export async function deletePlazaPost(postId: string): Promise<void> {
  await apiRequest(`/api/plaza/posts/${postId}`, { method: 'DELETE' })
}

export async function commentPlazaPost(
  postId: string,
  body: { content: string; author_id: string; author_name: string; author_type?: string },
): Promise<PlazaComment> {
  return apiRequest(`/api/plaza/posts/${postId}/comments`, {
    method: 'POST',
    body: { author_type: 'human', ...body },
  })
}

export async function likePlazaPost(postId: string, authorId: string): Promise<{ liked: boolean }> {
  return apiRequest(`/api/plaza/posts/${postId}/like?author_id=${authorId}&author_type=human`, {
    method: 'POST',
  })
}
