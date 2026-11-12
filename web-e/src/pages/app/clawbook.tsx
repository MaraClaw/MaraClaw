import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { useInfiniteQuery, useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { showRequestError } from '@/lib/request-error-toast'
import {
  commentClawbookPost,
  deleteClawbookPost,
  getClawbookPost,
  likeClawbookPost,
  clawbookStats,
  type ClawbookPost,
} from '@/lib/clawbook-api'
import { clawbookCommentOptions, clawbookCommentsKey, clawbookPostOptions, clawbookPublishOptions, distinctClawbookItems } from '@/lib/clawbook-query'

export function ClawbookPage() {
  const { t: translate, formatNumber } = useTranslation()
  const scope = useWorkspaceSession()
  const queryClient = useQueryClient()
  const [params] = useSearchParams()
  const focusedId = params.get('post')
  const [draft, setDraft] = useState('')

  const feed = useInfiniteQuery(clawbookPostOptions())
  const posts = distinctClawbookItems(feed.data?.pages)
  const stats = useQuery({ queryKey: ['clawbook', 'stats'], queryFn: clawbookStats })

  const publishOptions = clawbookPublishOptions(queryClient)
  const publish = useMutation({
    ...publishOptions,
    onSuccess() {
      setDraft('')
      toast.success(translate("Posted"))
      return publishOptions.onSuccess()
    },
    onError(error) {
      toast.error(userFacingRequestError(error, translate("Could not post")))
    },
  })

  return (
    <div className="mx-auto max-w-2xl space-y-5 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Clawbook</h1>
        <p className="text-sm text-muted-foreground">
          {translate("Company square for humans and company-wide agents.")}{stats.data ? ` ${formatNumber(stats.data.total_posts)} ${translate('posts')}.` : ''}
        </p>
      </div>

      <div className="space-y-2 rounded-2xl border border-border p-4 has-[textarea:focus-visible]:border-ring has-[textarea:focus-visible]:ring-2 has-[textarea:focus-visible]:ring-ring/40">
        <Textarea
          aria-label={translate("Share an update")}
          aria-describedby="clawbook-draft-count"
          className="rounded-none border-0 p-0 shadow-none focus-visible:ring-0"
          value={draft}
          onChange={scope.effect((event) => setDraft(event.target.value))}
          maxLength={1000}
          placeholder={translate("Share an update. Use @name to mention a teammate or agent.")}
        />
        <div className="flex items-center justify-between">
          <p id="clawbook-draft-count" className="text-xs tabular-nums text-muted-foreground">{formatNumber(draft.length)}/{formatNumber(1000)}</p>
          <Button size="sm" className="h-11" disabled={!draft.trim() || publish.isPending} onClick={() => publish.mutate({ content: draft.trim() })}>
            {translate("Post")}</Button>
        </div>
      </div>

      {feed.isLoading ? (
        <div className="flex h-24 items-center justify-center text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
        </div>
      ) : null}

      <ul className="space-y-3">
        {focusedId ? <FocusedPost key={focusedId} postId={focusedId} /> : null}
        {posts.filter((post) => post.id !== focusedId).map((post) => (
          <PostCard key={post.id} post={post} />
        ))}
      </ul>
      {feed.isError ? <RequestErrorToast error={feed.error} message="Could not load posts." /> : null}
      {feed.hasNextPage || feed.isError ? (
        <Button variant="outline" className="h-11" disabled={feed.isFetching} onClick={() => feed.hasNextPage ? feed.fetchNextPage() : feed.refetch()}>
          {feed.isError ? translate("Try again") : translate("Load more posts")}
        </Button>
      ) : null}
      {feed.isSuccess && posts.length === 0 ? <p className="text-sm text-muted-foreground">{translate("No posts yet. Start the feed.")}</p> : null}
    </div>
  )
}

function FocusedPost({ postId }: { postId: string }) {
  const { t: translate } = useTranslation()
  const detail = useQuery({ queryKey: ['clawbook', 'post', postId], queryFn: () => getClawbookPost(postId), retry: false })
  if (detail.isError) return <li><ThreadError error={detail.error} /></li>
  if (!detail.data) return <li role="status" className="text-sm text-muted-foreground">{translate("Loading post…")}</li>
  return <PostCard post={detail.data} expanded />
}

function ThreadError({ error }: { error: Error }) {
  return (
    <RequestErrorToast error={error} message={error instanceof ApiError && error.status === 404
        ? 'This post is unavailable. It may have been removed or is not available in this company.'
        : 'Could not load this post. Reload the page to try again.'} />
  )
}

function PostCard({ post, expanded = false }: { post: ClawbookPost; expanded?: boolean }) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(expanded)
  const canDelete = user?.role === 'org_admin'
  return (
    <li className="rounded-2xl border border-border p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">
            {post.author_name}{' '}
            <span className="font-normal text-muted-foreground">{post.author_type}</span>
          </p>
          <p className="mt-1 whitespace-pre-wrap text-sm">{post.content}</p>
        </div>
        {canDelete || post.author_id === user?.id ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              scope.isCurrent() && void deleteClawbookPost(post.id).then(() => {
                if (!scope.isCurrent()) return
                toast.success(translate("Removed"))
                void queryClient.invalidateQueries({ queryKey: ['clawbook'] })
              }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to delete this post.")), error)))
            }
          >
            {translate("Delete")}</Button>
        ) : null}
      </div>
      <div className="mt-3 flex gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            scope.isCurrent() && void likeClawbookPost(post.id).then((result) => {
              if (!scope.isCurrent()) return
              toast.success(result.liked ? translate("Liked") : translate("Unliked"))
              void queryClient.invalidateQueries({ queryKey: ['clawbook'] })
            }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to like this post.")), error)))
          }
        >
          {translate("Like")}{' '}{post.likes_count}
        </Button>
        <Button size="sm" variant="ghost" aria-expanded={open} onClick={scope.effect(() => setOpen(!open))}>
          {translate("Comments")}{' '}{post.comments_count}
        </Button>
      </div>
      {open ? <PostThread postId={post.id} /> : null}
    </li>
  )
}

function PostThread({ postId }: { postId: string }) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const comments = useInfiniteQuery({ ...clawbookCommentOptions(postId), retry: false })
  if (comments.isError && !comments.data) return <ThreadError error={comments.error} />
  if (!comments.data) return <p role="status" className="mt-3 text-sm text-muted-foreground">{translate("Loading replies…")}</p>

  return (
    <div className="mt-3 space-y-2 border-t border-border pt-3">
      {distinctClawbookItems(comments.data.pages).map((comment) => (
        <p key={comment.id} className="text-sm">
          <span className="font-medium">{comment.author_name}</span>{' '}
          <span className="text-muted-foreground">{comment.content}</span>
        </p>
      ))}
      {comments.isError ? <ThreadError error={comments.error} /> : null}
      {comments.hasNextPage ? (
        <Button variant="outline" size="sm" className="h-11" disabled={comments.isFetching} onClick={() => comments.fetchNextPage()}>
          {translate("Load more comments")}</Button>
      ) : null}
      <div className="space-y-2 rounded-2xl border border-border p-4 has-[textarea:focus-visible]:border-ring has-[textarea:focus-visible]:ring-2 has-[textarea:focus-visible]:ring-ring/40">
        <Textarea aria-label={translate("Reply")} value={text} onChange={scope.effect((event) => setText(event.target.value))} className="min-h-16 rounded-none border-0 p-0 shadow-none focus-visible:ring-0" maxLength={300} />
        <div className="flex justify-end">
          <Button
            size="sm"
            className="h-11"
            disabled={!text.trim()}
            onClick={() =>
              scope.isCurrent() && void commentClawbookPost(postId, {
                content: text.trim(),
              }).then(() => {
                if (!scope.isCurrent()) return
                setText('')
                void queryClient.invalidateQueries({ queryKey: clawbookCommentsKey(postId) })
                void queryClient.invalidateQueries({ queryKey: ['clawbook', 'posts'] })
              }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to send this reply.")), error)))
            }
          >
            {translate("Reply")}</Button>
        </div>
      </div>
    </div>
  )
}
