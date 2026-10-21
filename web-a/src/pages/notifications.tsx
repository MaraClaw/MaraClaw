import { formatDistanceToNowStrict } from 'date-fns'
import { ArrowRight, CheckCheck, Inbox } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useNotificationUnreadCount, useNotifications } from '@/hooks/use-notifications'
import { safeNotificationLink } from '@/lib/notification-links'
import { NOTIFICATIONS_PAGE_SIZE } from '@/lib/notifications-api'

const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function NotificationsPage() {
  const [offset, setOffset] = useState(0)
  const { notifications, markRead, markAllRead } = useNotifications(offset)
  const unread = useNotificationUnreadCount()
  const pending = markRead.isPending || markAllRead.isPending
  const items = notifications.data ?? []
  const hasUnread = (unread.data?.unread_count ?? 0) > 0 || items.some((item) => !item.is_read)
  const mutationError = markRead.isError || markAllRead.isError

  function resetErrors() {
    markRead.reset()
    markAllRead.reset()
  }

  return (
    <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">Inbox</h1>
          <p className="mt-1 text-sm leading-relaxed text-muted-foreground">Notifications for your admin account.</p>
        </div>
        <Button variant="outline" className={actionClass} disabled={pending || !hasUnread} onClick={() => { resetErrors(); markAllRead.mutate() }}>
          <CheckCheck className="size-4" aria-hidden />
          {markAllRead.isPending ? 'Marking all as read...' : 'Mark all as read'}
        </Button>
      </div>

      <p role="status" className="sr-only">{markRead.isSuccess ? 'Notification marked as read.' : markAllRead.isSuccess ? 'All notifications marked as read.' : ''}</p>
      {mutationError ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">Unable to mark notifications as read. Try again.</p> : null}
      {unread.isError ? <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
        <p>Unread count could not be updated.</p>
        <Button variant="outline" className={actionClass} disabled={unread.isFetching} onClick={() => { void unread.refetch() }}>Retry count</Button>
      </div> : null}
      {notifications.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">Loading notifications...</CardContent></Card> : null}
      {notifications.isError ? <Card><CardContent className="flex flex-wrap items-center justify-between gap-4">
        <p role="alert" className="text-sm">Unable to load notifications. Try again.</p>
        <Button variant="outline" className={actionClass} disabled={notifications.isFetching} onClick={() => { void notifications.refetch() }}>Retry</Button>
      </CardContent></Card> : null}

      {notifications.isSuccess && items.length === 0 ? <Card><CardContent className="flex flex-col items-center gap-3 py-6 text-center">
        <Inbox className="size-8 text-muted-foreground" aria-hidden />
        <h2 className="font-display text-lg font-semibold">{offset === 0 ? 'No notifications yet' : 'No more notifications'}</h2>
        <p className="text-sm leading-relaxed text-muted-foreground">{offset === 0 ? 'New updates will appear here. This Inbox refreshes automatically.' : 'Return to the previous page to see earlier updates.'}</p>
      </CardContent></Card> : null}

      {items.length > 0 ? <ul className="flex flex-col gap-4" aria-label="Notifications">
        {items.map((item) => {
          const link = safeNotificationLink(item.link)
          return <li key={item.id}>
            <Card>
              <CardContent className="flex flex-col gap-4">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant={item.is_read ? 'secondary' : 'soft'}>{item.is_read ? 'Read' : 'Unread'}</Badge>
                  {item.sender_name ? <span>{item.sender_name}</span> : null}
                  {item.created_at ? <time dateTime={item.created_at} title={new Date(item.created_at).toLocaleString()}>{formatDistanceToNowStrict(new Date(item.created_at), { addSuffix: true })}</time> : null}
                </div>
                <h2 className="font-display text-base font-semibold leading-snug">{item.title}</h2>
                {item.body ? <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{item.body}</p> : null}
                <div className="flex flex-wrap items-center gap-3">
                  {link ? <Button asChild variant="outline" className={actionClass}><Link to={link} aria-label={`Open notification: ${item.title}`}>Open notification<ArrowRight className="size-4" aria-hidden /></Link></Button> : null}
                  {!item.is_read ? <Button variant="secondary" className={actionClass} disabled={pending} aria-label={`Mark as read: ${item.title}`} onClick={() => { resetErrors(); markRead.mutate(item.id) }}>
                    {markRead.isPending && markRead.variables === item.id ? 'Marking as read...' : 'Mark as read'}
                  </Button> : null}
                </div>
              </CardContent>
            </Card>
          </li>
        })}
      </ul> : null}

      {offset > 0 || items.length > 0 ? <nav aria-label="Inbox pages" className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" className={actionClass} disabled={offset === 0 || notifications.isFetching || pending} onClick={() => setOffset((value) => Math.max(0, value - NOTIFICATIONS_PAGE_SIZE))}>Previous</Button>
        <p role="status" className="text-xs text-muted-foreground">Page {offset / NOTIFICATIONS_PAGE_SIZE + 1}</p>
        <Button variant="outline" className={actionClass} disabled={items.length < NOTIFICATIONS_PAGE_SIZE || notifications.isFetching || notifications.isError || pending} onClick={() => setOffset((value) => value + NOTIFICATIONS_PAGE_SIZE)}>Next</Button>
      </nav> : null}
    </div>
  )
}
