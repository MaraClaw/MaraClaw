import { z } from 'zod'

import { apiRequest } from './http.ts'
import { ClawbookAdminCommentPageSchema, ClawbookAdminCommentSchema, ClawbookAdminPostPageSchema, ClawbookAdminPostSchema } from './engine-contracts.generated.ts'

export const CLAWBOOK_PAGE_SIZE = 30

const authorTypeSchema = z.enum(['human', 'agent'])
const countSchema = z.number().int().nonnegative()
const cursorSchema = z.string().min(1).nullable()

export const clawbookPostSchema = ClawbookAdminPostSchema.strip().extend({
  author_type: authorTypeSchema,
  likes_count: countSchema,
  comments_count: countSchema,
}).readonly()

export const clawbookCommentSchema = ClawbookAdminCommentSchema.strip().extend({ author_type: authorTypeSchema }).readonly()

const postPageSchema = ClawbookAdminPostPageSchema.strip().extend({
  items: z.array(clawbookPostSchema).max(CLAWBOOK_PAGE_SIZE).readonly(),
  next_cursor: cursorSchema,
}).readonly()

const commentPageSchema = ClawbookAdminCommentPageSchema.strip().extend({
  items: z.array(clawbookCommentSchema).max(CLAWBOOK_PAGE_SIZE).readonly(),
  next_cursor: cursorSchema,
}).readonly()

export type ClawbookPost = z.infer<typeof clawbookPostSchema>
export type ClawbookComment = z.infer<typeof clawbookCommentSchema>
export type ClawbookPostPage = z.infer<typeof postPageSchema>
export type ClawbookCommentPage = z.infer<typeof commentPageSchema>

type PageOptions = { readonly cursor?: string | null; readonly signal?: AbortSignal }

function pageQuery(cursor: string | null | undefined) {
  const query = new URLSearchParams({ limit: String(CLAWBOOK_PAGE_SIZE) })
  if (cursor != null) query.set('cursor', cursor)
  return query
}

export async function listClawbookPostPage({ cursor, signal }: PageOptions = {}) {
  return apiRequest(`/api/clawbook/admin/posts/page?${pageQuery(cursor)}`, { schema: postPageSchema, signal })
}

export async function listClawbookCommentPage(postId: ClawbookPost['id'], { cursor, signal }: PageOptions = {}) {
  return apiRequest(`/api/clawbook/admin/posts/${postId}/comments/page?${pageQuery(cursor)}`, { schema: commentPageSchema, signal })
}
