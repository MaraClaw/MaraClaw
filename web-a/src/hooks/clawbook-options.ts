import { infiniteQueryOptions } from '@tanstack/react-query'

import { listClawbookCommentPage, listClawbookPostPage, type ClawbookPost } from '../lib/clawbook-api.ts'

type PageContext = { readonly pageParam: string | null; readonly signal: AbortSignal }

export const clawbookKeys = {
  all: ['admin-clawbook'] as const,
  posts: ['admin-clawbook', 'posts'] as const,
  comments: (postId: ClawbookPost['id']) => ['admin-clawbook', 'comments', postId] as const,
}

export function clawbookPostsOptions(enabled: boolean) {
  return infiniteQueryOptions({
    queryKey: clawbookKeys.posts, initialPageParam: null,
    queryFn: ({ pageParam, signal }: PageContext) => listClawbookPostPage({ cursor: pageParam, signal }),
    getNextPageParam: (page) => page.next_cursor,
    enabled, retry: false,
  })
}

export function clawbookCommentsOptions(postId: ClawbookPost['id'], enabled: boolean) {
  return infiniteQueryOptions({
    queryKey: clawbookKeys.comments(postId), initialPageParam: null,
    queryFn: ({ pageParam, signal }: PageContext) => listClawbookCommentPage(postId, { cursor: pageParam, signal }),
    getNextPageParam: (page) => page.next_cursor,
    enabled, retry: false,
  })
}
