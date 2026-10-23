import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { notificationListOptions, notificationReadOptions, notificationsReadAllOptions, notificationUnreadOptions } from '@/hooks/notification-options'
import { useInfiniteQuery, useMutation, useQuery } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { notificationCategories, notificationCategorySchema, type NotificationCategory } from '@/lib/notifications-api'
import { relativeTime } from '@/lib/relative-time'

const actionClass = 'h-auto min-h-11 whitespace-normal'
const categoryLabels = { all: 'All', tool: 'Tools', approval: 'Approvals', social: 'Social', broadcast: 'Broadcasts' } satisfies Record<NotificationCategory, string>

function memberLink(link?: string | null): string | null {
  if (!link) return null
  if (link.startsWith('/plaza')) return `/app${link}`
  return link
}

export function NotificationsPage() {
  const [category, setCategory] = useState<NotificationCategory>('all')
  const queryClient = useQueryClient()
  const query = useInfiniteQuery(notificationListOptions(category))
  const unreadQuery = useQuery(notificationUnreadOptions(category))
  const markRead = useMutation(notificationReadOptions(queryClient))
  const markAll = useMutation(notificationsReadAllOptions(queryClient))
  const pending = markRead.isPending || markAll.isPending
  const hasUnread = (unreadQuery.data ?? 0) > 0
  const items = query.data?.pages.flat() ?? []

  function resetErrors() {
    markRead.reset()
    markAll.reset()
  }

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">Inbox</h1>
        <Button variant="outline" className={actionClass} aria-describedby="notification-bulk-scope" disabled={pending || !hasUnread} onClick={() => { resetErrors(); markAll.mutate(category) }}>
          {markAll.isPending ? 'Marking all read…' : 'Mark all read'}
        </Button>
      </div>
      <div className="space-y-2">
        <Label htmlFor="notification-category">Category</Label>
        <Select id="notification-category" value={category} onChange={(event) => { resetErrors(); setCategory(notificationCategorySchema.parse(event.currentTarget.value)) }}>
          {notificationCategories.map((value) => <option key={value} value={value}>{categoryLabels[value]}</option>)}
        </Select>
        <p id="notification-bulk-scope" className="text-sm text-muted-foreground">Mark all read covers ALL notifications matching this category, including those not loaded.</p>
        <p role="status" className="text-sm text-muted-foreground">{unreadQuery.data === undefined ? 'Checking unread notifications…' : `${unreadQuery.data} unread in ${categoryLabels[category]}`}</p>
        {unreadQuery.isError ? <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">Unable to check unread notifications.</p>
          <Button variant="outline" className={actionClass} disabled={unreadQuery.isFetching} onClick={() => void unreadQuery.refetch()}>Retry unread count</Button>
        </div> : null}
      </div>
      <p role="status" className="sr-only">{markRead.isSuccess ? 'Notification marked as read.' : markAll.isSuccess ? `All notifications in ${categoryLabels[markAll.variables]} marked as read.` : ''}</p>
      {markRead.isError || markAll.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(markRead.error ?? markAll.error, 'Unable to mark notifications as read. Try again.')}</p> : null}
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading notifications…</p> : null}
      {query.isError && !query.isFetchNextPageError ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load notifications. Check your connection and try again.')}</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.refetch()}>Retry</Button>
      </div> : null}
      <ul aria-label="Notifications" className="space-y-2">
        {items.map((item) => {
          const link = memberLink(item.link)
          return <li key={item.id} className="space-y-3 rounded-xl border border-border px-3 py-3">
            <div className="space-y-1">
              <p className="text-sm font-medium">{item.title}</p>
              {item.body ? <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{item.body}</p> : null}
              {item.created_at ? <time className="block text-xs text-muted-foreground" dateTime={item.created_at} title={new Date(item.created_at).toLocaleString()}>{relativeTime(item.created_at)}</time> : null}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {link ? <Button asChild variant="outline" className={actionClass}><Link to={link} aria-label={`Open notification: ${item.title}`}>Open notification</Link></Button> : null}
              {!item.is_read ? <Button variant="ghost" className={actionClass} disabled={pending} aria-label={`Mark as read: ${item.title}`} onClick={() => { resetErrors(); markRead.mutate(item.id) }}>
                {markRead.isPending && markRead.variables === item.id ? 'Marking as read…' : 'Mark as read'}
              </Button> : <span className="text-xs text-muted-foreground">Read</span>}
            </div>
          </li>
        })}
      </ul>
      {query.isFetchNextPageError ? <p role="alert" className="text-sm text-destructive">Unable to load more notifications. Try again.</p> : null}
      {query.hasNextPage ? <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.fetchNextPage()}>
        {query.isFetchingNextPage ? 'Loading more…' : query.isFetchNextPageError ? 'Retry load more' : 'Load more'}
      </Button> : null}
      {query.isSuccess && items.length === 0 ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p className="text-sm text-muted-foreground">No notifications yet. Refresh to check for new updates.</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh Inbox</Button>
      </div> : null}
    </div>
  )
}
