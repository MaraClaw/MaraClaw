import { useInfiniteQuery } from '@tanstack/react-query'

import type { ClawbookPost } from '@/lib/clawbook-api'
import { canViewClawbook } from '@/lib/types/auth-guards'
import { clawbookCommentsOptions, clawbookPostsOptions } from './clawbook-options'
import { useAuth } from './use-auth'

export function useClawbookPosts() {
  const { user, mustChangePassword } = useAuth()
  return useInfiniteQuery(clawbookPostsOptions(canViewClawbook(user) && !mustChangePassword))
}

export function useClawbookComments(postId: ClawbookPost['id'], open: boolean) {
  const { user, mustChangePassword } = useAuth()
  return useInfiniteQuery(clawbookCommentsOptions(postId, open && canViewClawbook(user) && !mustChangePassword))
}
