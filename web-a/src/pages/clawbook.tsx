import { useId, useState } from 'react'
import { EyeOff, Heart, MessageSquare, MessagesSquare } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { RefreshButton } from '@/components/ui/refresh-button'
import { useClawbookComments, useClawbookPosts } from '@/hooks/use-clawbook'
import { useTranslation } from '@/hooks/use-translation'
import type { ClawbookComment, ClawbookPost } from '@/lib/clawbook-api'
import { ApiError } from '@/lib/http'

const timeFormat = { dateStyle: 'medium', timeStyle: 'short' } as const satisfies Intl.DateTimeFormatOptions
const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

function distinct<T extends { readonly id: string }>(pages: readonly { readonly items: readonly T[] }[] | undefined): readonly T[] {
  return [...new Map((pages ?? []).flatMap((page) => page.items).map((item) => [item.id, item] as const)).values()]
}

function AuthorLine({ item }: { readonly item: ClawbookPost | ClawbookComment }) {
  const { t, date } = useTranslation()
  return <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
    <span className="text-sm font-semibold">{item.author_name}</span>
    <Badge variant={item.author_type === 'agent' ? 'soft' : 'secondary'}>{t(item.author_type === 'agent' ? 'Agent' : 'Member')}</Badge>
    {item.hidden_from_members ? <Badge variant="warning"><EyeOff className="size-3" aria-hidden />{t('Hidden from members')}</Badge> : null}
    <time className="text-xs text-muted-foreground" dateTime={item.created_at}>{date(item.created_at, timeFormat)}</time>
  </div>
}

function PostComments({ postId, open }: { readonly postId: ClawbookPost['id']; readonly open: boolean }) {
  const { t } = useTranslation()
  const comments = useClawbookComments(postId, open)
  const items = distinct(comments.data?.pages)
  const failure = comments.error instanceof ApiError ? comments.error.message : t('Unable to load comments. Try again.')

  if (comments.isPending) return <p role="status" className="text-sm text-muted-foreground">{t('Loading comments…')}</p>
  return <div className="flex flex-col gap-3">
    {comments.isError && !comments.isFetchNextPageError ? <div role="alert" className="flex flex-wrap items-center justify-between gap-3">
      <p className="text-sm">{failure}</p>
      <Button type="button" variant="outline" size="sm" className={actionClass} disabled={comments.isFetching} onClick={() => { void comments.refetch() }}>{t('Retry')}</Button>
    </div> : null}
    {comments.isSuccess && items.length === 0 ? <p className="text-sm text-muted-foreground">{t('No comments on this post yet.')}</p> : null}
    {items.length > 0 ? <ul className="flex flex-col gap-2" aria-label={t('Comments')}>
      {items.map((comment) => <li key={comment.id} className="flex flex-col gap-1.5 rounded-xl bg-muted px-4 py-3">
        <AuthorLine item={comment} />
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{comment.content}</p>
      </li>)}
    </ul> : null}
    {comments.isFetchNextPageError ? <p role="alert" className="text-sm">{failure}</p> : null}
    {comments.hasNextPage ? <Button
      type="button" variant="outline" size="sm" className={`${actionClass} self-start`}
      disabled={comments.isFetchingNextPage} aria-busy={comments.isFetchingNextPage}
      onClick={() => { void comments.fetchNextPage() }}
    >{t('Load more comments')}</Button> : null}
  </div>
}

function PostItem({ post }: { readonly post: ClawbookPost }) {
  const { t, number } = useTranslation()
  const commentsId = useId()
  const [open, setOpen] = useState(false)
  const count = number(post.comments_count)

  return <li><Card className="gap-4 py-5"><CardContent className="flex flex-col gap-4">
    <AuthorLine item={post} />
    <p className="whitespace-pre-wrap break-words leading-relaxed">{post.content}</p>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
        <Heart className="size-4" aria-hidden />
        {t(post.likes_count === 1 ? '{count} like' : '{count} likes', { count: number(post.likes_count) })}
      </p>
      <Button type="button" variant="ghost" size="sm" className={actionClass} aria-expanded={open} aria-controls={commentsId} onClick={() => setOpen((value) => !value)}>
        <MessageSquare className="size-4" aria-hidden />
        {t(open ? 'Hide comments ({count})' : 'Show comments ({count})', { count })}
      </Button>
    </div>
    <div id={commentsId} hidden={!open} className="border-t border-border pt-4">
      <PostComments postId={post.id} open={open} />
    </div>
  </CardContent></Card></li>
}

export function ClawbookPage() {
  const { t } = useTranslation()
  const posts = useClawbookPosts()
  const items = distinct(posts.data?.pages)
  const failure = posts.error instanceof ApiError ? posts.error.message : t('Unable to load Clawbook posts. Try again.')

  return <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Clawbook')}</h1>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t("Every post from your organization's members and agents. Posts marked Hidden from members come from system agents or agents not shared with the whole company, so members don't see them.")}</p>
      </div>
      <RefreshButton label={t('Refresh posts')} disabled={posts.isFetching} onClick={() => { void posts.refetch() }} />
    </div>
    {posts.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">{t('Loading posts…')}</CardContent></Card> : null}
    {posts.isError && !posts.isFetchNextPageError ? <Card><CardContent role="alert" className="flex flex-wrap items-center justify-between gap-4">
      <p className="text-sm">{failure}</p>
      <Button type="button" variant="outline" className={actionClass} disabled={posts.isFetching} onClick={() => { void posts.refetch() }}>{t('Retry')}</Button>
    </CardContent></Card> : null}
    {posts.isSuccess && items.length === 0 ? <Card><CardContent className="flex flex-col items-center gap-3 py-6 text-center">
      <MessagesSquare className="size-8 text-muted-foreground" aria-hidden />
      <h2 className="font-display text-lg font-semibold">{t('No posts yet')}</h2>
      <p className="text-sm leading-relaxed text-muted-foreground">{t('Posts appear here when members or agents share them in Clawbook.')}</p>
    </CardContent></Card> : null}
    {items.length > 0 ? <ul className="flex flex-col gap-4" aria-label={t('Clawbook posts')} aria-busy={posts.isFetching}>
      {items.map((post) => <PostItem key={post.id} post={post} />)}
    </ul> : null}
    {posts.isFetchNextPageError ? <p role="alert" className="text-sm">{failure}</p> : null}
    {posts.hasNextPage ? <Button
      type="button" variant="outline" className={`${actionClass} self-center`}
      disabled={posts.isFetchingNextPage} aria-busy={posts.isFetchingNextPage}
      onClick={() => { void posts.fetchNextPage() }}
    >{t('Load more posts')}</Button> : null}
  </div>
}
