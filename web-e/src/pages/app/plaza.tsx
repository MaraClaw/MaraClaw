import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  commentPlazaPost,
  createPlazaPost,
  deletePlazaPost,
  getPlazaPost,
  likePlazaPost,
  listPlazaPosts,
  plazaStats,
  type PlazaPost,
} from '@/lib/plaza-api'

export function PlazaPage() {
  const scope = useWorkspaceSession()
  const queryClient = useQueryClient()
  const [params] = useSearchParams()
  const focusedId = params.get('post')
  const [draft, setDraft] = useState('')

  const feed = useQuery({ queryKey: ['plaza', 'posts'], queryFn: () => listPlazaPosts() })
  const stats = useQuery({ queryKey: ['plaza', 'stats'], queryFn: plazaStats })

  const publish = useMutation({
    mutationFn: () => createPlazaPost({ content: draft.trim() }),
    onSuccess() {
      setDraft('')
      toast.success('Posted')
      void queryClient.invalidateQueries({ queryKey: ['plaza'] })
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Could not post')
    },
  })

  return (
    <div className="mx-auto max-w-2xl space-y-5 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Plaza</h1>
        <p className="text-sm text-muted-foreground">
          Company square for humans and company-wide agents.
          {stats.data ? ` ${stats.data.total_posts} posts.` : ''}
        </p>
      </div>

      <div className="space-y-2 rounded-2xl border border-border p-4">
        <Textarea
          aria-label="Share an update"
          value={draft}
          onChange={scope.effect((event) => setDraft(event.target.value))}
          maxLength={500}
          placeholder="Share an update. Use @name to mention a teammate or agent."
        />
        <div className="flex items-center justify-between">
          <p className="text-xs text-muted-foreground">{draft.length}/500</p>
          <Button size="sm" disabled={!draft.trim() || publish.isPending} onClick={() => publish.mutate()}>
            Post
          </Button>
        </div>
      </div>

      {feed.isLoading ? (
        <div className="flex h-24 items-center justify-center text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
        </div>
      ) : null}

      <ul className="space-y-3">
        {focusedId ? <FocusedPost key={focusedId} postId={focusedId} /> : null}
        {(feed.data ?? []).filter((post) => post.id !== focusedId).map((post) => (
          <PostCard key={post.id} post={post} />
        ))}
      </ul>
      {feed.data?.length === 0 ? <p className="text-sm text-muted-foreground">No posts yet. Start the feed.</p> : null}
    </div>
  )
}

function FocusedPost({ postId }: { postId: string }) {
  const detail = useQuery({ queryKey: ['plaza', 'post', postId], queryFn: () => getPlazaPost(postId), retry: false })
  if (detail.isError) return <li><ThreadError error={detail.error} /></li>
  if (!detail.data) return <li role="status" className="text-sm text-muted-foreground">Loading post…</li>
  return <PostCard post={detail.data} expanded />
}

function ThreadError({ error }: { error: Error }) {
  return (
    <p role="status" className="text-sm text-muted-foreground">
      {error instanceof ApiError && error.status === 404
        ? 'This post is unavailable. It may have been removed or is not available in this company.'
        : 'Could not load this post. Reload the page to try again.'}
    </p>
  )
}

function PostCard({ post, expanded = false }: { post: PlazaPost; expanded?: boolean }) {
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
              scope.isCurrent() && void deletePlazaPost(post.id).then(() => {
                if (!scope.isCurrent()) return
                toast.success('Removed')
                void queryClient.invalidateQueries({ queryKey: ['plaza'] })
              }).catch(scope.rethrowCurrent)
            }
          >
            Delete
          </Button>
        ) : null}
      </div>
      <div className="mt-3 flex gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            scope.isCurrent() && void likePlazaPost(post.id).then((result) => {
              if (!scope.isCurrent()) return
              toast.success(result.liked ? 'Liked' : 'Unliked')
              void queryClient.invalidateQueries({ queryKey: ['plaza'] })
            }).catch(scope.rethrowCurrent)
          }
        >
          Like {post.likes_count}
        </Button>
        <Button size="sm" variant="ghost" aria-expanded={open} onClick={scope.effect(() => setOpen(!open))}>
          Comments {post.comments_count}
        </Button>
      </div>
      {open ? <PostThread postId={post.id} /> : null}
    </li>
  )
}

function PostThread({ postId }: { postId: string }) {
  const scope = useWorkspaceSession()
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const detail = useQuery({ queryKey: ['plaza', 'post', postId], queryFn: () => getPlazaPost(postId), retry: false })
  if (detail.isError) return <ThreadError error={detail.error} />
  if (!detail.data) return <p role="status" className="mt-3 text-sm text-muted-foreground">Loading replies…</p>

  return (
    <div className="mt-3 space-y-2 border-t border-border pt-3">
      {detail.data.comments.map((comment) => (
        <p key={comment.id} className="text-sm">
          <span className="font-medium">{comment.author_name}</span>{' '}
          <span className="text-muted-foreground">{comment.content}</span>
        </p>
      ))}
      <div className="flex gap-2">
        <Textarea aria-label="Reply" value={text} onChange={scope.effect((event) => setText(event.target.value))} className="min-h-16" maxLength={300} />
        <Button
          size="sm"
          disabled={!text.trim()}
          onClick={() =>
            scope.isCurrent() && void commentPlazaPost(postId, {
              content: text.trim(),
            }).then(() => {
              if (!scope.isCurrent()) return
              setText('')
              void queryClient.invalidateQueries({ queryKey: ['plaza'] })
            }).catch(scope.rethrowCurrent)
          }
        >
          Reply
        </Button>
      </div>
    </div>
  )
}
