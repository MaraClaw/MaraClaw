import { useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { notificationReadOptions, notificationsReadAllOptions } from '@/hooks/notification-options'
import { useMutation, useQuery } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'
import { listNotifications } from '@/lib/workspace-api'

const actionClass = 'h-auto min-h-11 whitespace-normal'

function memberLink(link?: string | null): string | null {
  if (!link) return null
  if (link.startsWith('/plaza')) return `/app${link}`
  return link
}

export function NotificationsPage() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['notifications'], queryFn: listNotifications })
  const markRead = useMutation(notificationReadOptions(queryClient))
  const markAll = useMutation(notificationsReadAllOptions(queryClient))
  const pending = markRead.isPending || markAll.isPending
  const hasUnread = query.data?.some((item) => !item.is_read) ?? false

  function resetErrors() {
    markRead.reset()
    markAll.reset()
  }

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">Inbox</h1>
        <Button variant="outline" className={actionClass} disabled={pending || !hasUnread} onClick={() => { resetErrors(); markAll.mutate() }}>
          {markAll.isPending ? 'Marking all read…' : 'Mark all read'}
        </Button>
      </div>
      <p role="status" className="sr-only">{markRead.isSuccess ? 'Notification marked as read.' : markAll.isSuccess ? 'All notifications marked as read.' : ''}</p>
      {markRead.isError || markAll.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(markRead.error ?? markAll.error, 'Unable to mark notifications as read. Try again.')}</p> : null}
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading notifications…</p> : null}
      {query.isError ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load notifications. Check your connection and try again.')}</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.refetch()}>Retry</Button>
      </div> : null}
      <ul aria-label="Notifications" className="space-y-2">
        {query.data?.map((item) => {
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
      {query.isSuccess && query.data.length === 0 ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p className="text-sm text-muted-foreground">No notifications yet. Refresh to check for new updates.</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh Inbox</Button>
      </div> : null}
    </div>
  )
}
