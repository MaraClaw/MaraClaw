import type { InfiniteData, QueryClient } from '@tanstack/react-query'
import {
  createClawbookPost, listClawbookCommentPage, listClawbookPostPage,
  type ClawbookPage, type ClawbookPost,
} from './clawbook-api.ts'

export const clawbookPostsKey = ['clawbook', 'posts'] as const
export const clawbookCommentsKey = (postId: string) => ['clawbook', 'comments', postId] as const

export function clawbookPostOptions() {
  return {
    queryKey: clawbookPostsKey,
    initialPageParam: null,
    queryFn: ({ pageParam, signal }: { pageParam: string | null; signal: AbortSignal }) => listClawbookPostPage({ cursor: pageParam, signal }),
    getNextPageParam: (page: ClawbookPage<ClawbookPost>) => page.next_cursor ?? undefined,
  }
}

export function clawbookCommentOptions(postId: string) {
  return {
    queryKey: clawbookCommentsKey(postId),
    initialPageParam: null,
    queryFn: ({ pageParam, signal }: { pageParam: string | null; signal: AbortSignal }) => listClawbookCommentPage(postId, { cursor: pageParam, signal }),
    getNextPageParam: (page: ClawbookPage<{ id: string }>) => page.next_cursor ?? undefined,
  }
}

export function distinctClawbookItems<T extends { readonly id: string }>(pages: readonly ClawbookPage<T>[] | undefined): T[] {
  const seen = new Set<string>()
  return (pages ?? []).flatMap((page) => page.items.filter((item) => {
    if (seen.has(item.id)) return false
    seen.add(item.id)
    return true
  }))
}

export function clawbookPublishOptions(client: QueryClient) {
  return {
    mutationFn: createClawbookPost,
    onSuccess() {
      void client.cancelQueries({ queryKey: clawbookPostsKey })
      client.setQueryData<InfiniteData<ClawbookPage<ClawbookPost>, string | null>>(clawbookPostsKey, (data) =>
        data && { pages: data.pages.slice(0, 1), pageParams: data.pageParams.slice(0, 1) })
      return Promise.all([
        client.invalidateQueries({ queryKey: clawbookPostsKey, refetchType: 'all' }),
        client.invalidateQueries({ queryKey: ['clawbook', 'stats'] }),
      ])
    },
  }
}
