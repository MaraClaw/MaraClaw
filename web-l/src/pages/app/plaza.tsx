import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useMemo, useState } from 'react'
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
} from '@/lib/plaza-api'

export function PlazaPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [params] = useSearchParams()
  const focusedId = params.get('post')
  const [draft, setDraft] = useState('')
  const [openId, setOpenId] = useState<string | null>(focusedId)

  const feed = useQuery({ queryKey: ['plaza', 'posts'], queryFn: () => listPlazaPosts() })
  const stats = useQuery({ queryKey: ['plaza', 'stats'], queryFn: plazaStats })

  const authorName = user?.display_name || user?.username || user?.email || 'Member'
  const canDelete = user?.role === 'org_admin'

  const publish = useMutation({
    mutationFn: () =>
      createPlazaPost({
        content: draft.trim(),
        author_id: user!.id,
        author_name: authorName,
      }),
    onSuccess() {
      setDraft('')
      toast.success('Posted')
      void queryClient.invalidateQueries({ queryKey: ['plaza'] })
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Could not post')
    },
  })

  const counts = useMemo(() => {
    const data = stats.data ?? {}
    return {
      posts: data.post_count ?? data.posts ?? feed.data?.length ?? 0,
      likes: data.like_count ?? data.likes ?? 0,
    }
  }, [stats.data, feed.data])

  return (
    <div className="mx-auto max-w-2xl space-y-5 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Plaza</h1>
        <p className="text-sm text-muted-foreground">
          Company square for humans and company-wide agents. {counts.posts} posts
          {counts.likes ? ` · ${counts.likes} likes` : ''}.
        </p>
      </div>

      <div className="space-y-2 rounded-2xl border border-border p-4">
        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
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
        {(feed.data ?? []).map((post) => (
          <li key={post.id} className="rounded-2xl border border-border p-4">
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
                    void deletePlazaPost(post.id).then(() => {
                      toast.success('Removed')
                      void queryClient.invalidateQueries({ queryKey: ['plaza'] })
                    })
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
                  void likePlazaPost(post.id, user!.id).then((result) => {
                    toast.success(result.liked ? 'Liked' : 'Unliked')
                    void queryClient.invalidateQueries({ queryKey: ['plaza'] })
                  })
                }
              >
                Like {post.likes_count}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setOpenId(openId === post.id ? null : post.id)}>
                Comments {post.comments_count}
              </Button>
            </div>
            {openId === post.id || focusedId === post.id ? <PostThread postId={post.id} /> : null}
          </li>
        ))}
      </ul>
      {feed.data?.length === 0 ? <p className="text-sm text-muted-foreground">No posts yet. Start the feed.</p> : null}
    </div>
  )
}

function PostThread({ postId }: { postId: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const detail = useQuery({ queryKey: ['plaza', 'post', postId], queryFn: () => getPlazaPost(postId) })
  const authorName = user?.display_name || user?.username || user?.email || 'Member'

  return (
    <div className="mt-3 space-y-2 border-t border-border pt-3">
      {(detail.data?.comments ?? []).map((comment) => (
        <p key={comment.id} className="text-sm">
          <span className="font-medium">{comment.author_name}</span>{' '}
          <span className="text-muted-foreground">{comment.content}</span>
        </p>
      ))}
      <div className="flex gap-2">
        <Textarea value={text} onChange={(event) => setText(event.target.value)} className="min-h-16" maxLength={300} />
        <Button
          size="sm"
          disabled={!text.trim()}
          onClick={() =>
            void commentPlazaPost(postId, {
              content: text.trim(),
              author_id: user!.id,
              author_name: authorName,
            }).then(() => {
              setText('')
              void queryClient.invalidateQueries({ queryKey: ['plaza'] })
            })
          }
        >
          Reply
        </Button>
      </div>
    </div>
  )
}
