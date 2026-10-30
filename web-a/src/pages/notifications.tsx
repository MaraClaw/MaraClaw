import { formatDistanceToNowStrict } from 'date-fns'
import { Archive, ArchiveRestore, ArrowRight, CheckCheck, Inbox } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useNotificationUnreadCount, useNotifications } from '@/hooks/use-notifications'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { safeNotificationLink } from '@/lib/notification-links'
import { type NotificationId, type NotificationView } from '@/lib/notifications-api'
import { canReviewAgentDeletions } from '@/lib/types/auth'

const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function NotificationsPage() {
  const { user } = useAuth()
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null])
  const [view, setView] = useState<NotificationView>('active')
  const [selected, setSelected] = useState<readonly NotificationId[]>([])
  const [selectedError, setSelectedError] = useState<string | null>(null)
  const selectionOwner = useRef({})
  const heading = useRef<HTMLHeadingElement>(null)
  const isCurrent = useSessionCurrent()
  const { notifications, markRead, markAllRead, markSelectedRead, archive, restore, archiveSelected, restoreSelected, resetFirstPage } = useNotifications(cursors.at(-1) ?? null, view)
  const moveSelected = view === 'active' ? archiveSelected : restoreSelected
  const unread = useNotificationUnreadCount()
  const pending = markRead.isPending || markAllRead.isPending || markSelectedRead.isPending || archive.isPending || restore.isPending || archiveSelected.isPending || restoreSelected.isPending
  const items = [...new Map(notifications.data?.items.map((item) => [item.id, item])).values()]
  useEffect(() => {
    if (markRead.isSuccess || markAllRead.isSuccess || markSelectedRead.isSuccess || archive.isSuccess || restore.isSuccess || archiveSelected.isSuccess || restoreSelected.isSuccess) setCursors([null])
  }, [markRead.isSuccess, markAllRead.isSuccess, markSelectedRead.isSuccess, archive.isSuccess, restore.isSuccess, archiveSelected.isSuccess, restoreSelected.isSuccess])
  const selectedIds = items.filter((item) => selected.includes(item.id)).slice(0, 200).map((item) => item.id)
  const hasUnread = (unread.data?.unread_count ?? 0) > 0 || items.some((item) => !item.is_read && item.archived_at === null)
  const mutationError = archive.isError ? 'Unable to archive this notification. Try again.'
    : restore.isError ? 'Unable to restore this notification. Try again.'
    : selectedError ? selectedError
    : markRead.isError || markAllRead.isError ? 'Unable to mark notifications as read. Try again.' : null

  function resetErrors() {
    setSelectedError(null)
    for (const mutation of [markRead, markAllRead, markSelectedRead, archive, restore, archiveSelected, restoreSelected]) {
      if (!mutation.isPending) mutation.reset()
    }
  }

  function select(ids: readonly NotificationId[]) {
    selectionOwner.current = {}
    setSelectedError(null)
    setSelected(ids)
  }

  function changePage(nextCursors: readonly (string | null)[]) {
    select([])
    resetErrors()
    setCursors(nextCursors)
  }

  function readSelected() {
    if (pending || selectedIds.length === 0 || !isCurrent()) return
    const owner = selectionOwner.current
    const ids = [...selectedIds]
    resetErrors()
    markSelectedRead.mutate(ids, {
      onSuccess: () => { if (isCurrent() && selectionOwner.current === owner) { select([]); focusInbox() } },
      onError: () => { if (isCurrent() && selectionOwner.current === owner) setSelectedError('Unable to mark selected notifications as read. Your selection is kept. Try again.') },
    })
  }

  function moveSelection() {
    if (pending || selectedIds.length === 0 || !isCurrent()) return
    const owner = selectionOwner.current
    resetErrors()
    moveSelected.mutate([...selectedIds], {
      onSuccess: () => { if (isCurrent() && selectionOwner.current === owner) { select([]); focusInbox() } },
      onError: () => { if (isCurrent() && selectionOwner.current === owner) setSelectedError('Unable to move selected notifications. Your selection is kept. Try again.') },
    })
  }

  function focusInbox() {
    if (isCurrent()) heading.current?.focus()
  }

  return (
    <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 ref={heading} tabIndex={-1} className="font-display text-2xl font-semibold tracking-tight focus-visible:outline focus-visible:outline-ring">Inbox</h1>
          <p className="mt-1 text-sm leading-relaxed text-muted-foreground">Notifications for your admin account. Only active unread notifications count toward your badge.</p>
        </div>
        <div className="flex flex-wrap gap-3">
          {view === 'active' ? <Button variant="outline" className={actionClass} disabled={pending || !hasUnread} onClick={() => { resetErrors(); markAllRead.mutate() }}>
            <CheckCheck className="size-4" aria-hidden />
            {markAllRead.isPending ? 'Marking all as read...' : 'Mark all as read'}
          </Button> : null}
          <Button variant="outline" className={actionClass} disabled={notifications.isFetching || pending} onClick={() => { changePage([null]); void resetFirstPage() }}>Refresh Inbox</Button>
        </div>
      </div>

      <fieldset className="flex flex-wrap gap-3">
        <legend className="sr-only">Inbox view</legend>
        {(['active', 'archived'] as const).map((value) => <Button key={value} variant={view === value ? 'secondary' : 'outline'} className={actionClass}
          aria-pressed={view === value} onClick={() => { if (value !== view) { changePage([null]); void resetFirstPage(value); setView(value) } }}>
          {value === 'active' ? <Inbox className="size-4" aria-hidden /> : <Archive className="size-4" aria-hidden />}
          {value === 'active' ? 'Active' : 'Archived'}
        </Button>)}
      </fieldset>

      {items.length > 0 ? <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex min-h-11 cursor-pointer items-center gap-3 text-sm">
          <input type="checkbox" className="size-4 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            checked={selectedIds.length === Math.min(items.length, 200)} onChange={(event) => select(event.target.checked ? items.slice(0, 200).map((item) => item.id) : [])} />
          Select this page
        </label>
        <Button variant="outline" className={`${actionClass} disabled:text-muted-foreground disabled:opacity-100`} disabled={pending || selectedIds.length === 0} onClick={readSelected}>
          <CheckCheck className="size-4" aria-hidden />
          {markSelectedRead.isPending ? 'Marking selected as read...' : `Mark selected as read (${selectedIds.length})`}
        </Button>
        <Button variant="outline" className={actionClass} disabled={pending || selectedIds.length === 0} onClick={moveSelection}>
          {view === 'active' ? <Archive className="size-4" aria-hidden /> : <ArchiveRestore className="size-4" aria-hidden />}
          {view === 'active' ? archiveSelected.isPending ? 'Archiving selected...' : `Archive selected (${selectedIds.length})` : restoreSelected.isPending ? 'Restoring selected...' : `Restore selected (${selectedIds.length})`}
        </Button>
        <p role="status" className="w-full text-xs text-muted-foreground sm:text-sm">{selectedIds.length} selected on this page. Changing page or view clears the selection.</p>
      </div> : null}

      <p role="status" aria-atomic="true" className="sr-only">{archiveSelected.isSuccess ? 'Selected notifications archived. Read state unchanged.' : restoreSelected.isSuccess ? 'Selected notifications restored. Read state unchanged.' : archive.isSuccess ? 'Notification archived. Read state unchanged.' : restore.isSuccess ? 'Notification restored. Read state unchanged.' : markSelectedRead.isSuccess ? 'Selected notifications marked as read.' : markRead.isSuccess ? 'Notification marked as read.' : markAllRead.isSuccess ? 'All active notifications marked as read.' : ''}</p>
      {mutationError ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">{mutationError}</p> : null}
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
        <h2 className="font-display text-lg font-semibold">{cursors.length > 1 ? 'No more notifications' : view === 'active' ? 'No active notifications' : 'No archived notifications'}</h2>
        <p className="text-sm leading-relaxed text-muted-foreground">{cursors.length > 1 ? 'Return to the previous page to see earlier updates.' : view === 'active' ? 'New updates will appear here. Check Archived for saved notifications.' : 'Archive an active notification to keep it here without changing its read state.'}</p>
      </CardContent></Card> : null}

      {items.length > 0 ? <ul className="flex flex-col gap-4" aria-label="Notifications" aria-busy={pending}>
        {items.map((item) => {
          const safeLink = safeNotificationLink(item.link)
          const link = safeLink?.startsWith('/agent-deletion-requests') && !canReviewAgentDeletions(user) ? null : safeLink
          return <li key={item.id}>
            <Card className={item.is_read ? 'bg-card' : 'bg-accent'}>
              <CardContent className="flex flex-col gap-4">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <label className="flex min-h-11 cursor-pointer items-center gap-3 pr-2 text-sm text-foreground">
                    <input type="checkbox" className="size-4 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                      aria-label={`Select notification: ${item.title}`} checked={selectedIds.includes(item.id)}
                      disabled={!selectedIds.includes(item.id) && selectedIds.length >= 200}
                      onChange={(event) => select(event.target.checked ? [...selectedIds, item.id] : selectedIds.filter((id) => id !== item.id))} />
                    Select
                  </label>
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
                  {item.archived_at === null ? <Button variant="outline" className={actionClass} disabled={pending} aria-label={`Archive notification: ${item.title}`} onClick={() => { resetErrors(); archive.mutate(item.id, { onSuccess: focusInbox }) }}>
                    <Archive className="size-4" aria-hidden />
                    {archive.isPending && archive.variables === item.id ? 'Archiving...' : 'Archive'}
                  </Button> : <Button variant="outline" className={actionClass} disabled={pending} aria-label={`Restore notification: ${item.title}`} onClick={() => { resetErrors(); restore.mutate(item.id, { onSuccess: focusInbox }) }}>
                    <ArchiveRestore className="size-4" aria-hidden />
                    {restore.isPending && restore.variables === item.id ? 'Restoring...' : 'Restore'}
                  </Button>}
                </div>
              </CardContent>
            </Card>
          </li>
        })}
      </ul> : null}

      {cursors.length > 1 || items.length > 0 ? <nav aria-label="Inbox pages" className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" className={actionClass} disabled={cursors.length === 1 || notifications.isFetching} onClick={() => changePage(cursors.slice(0, -1))}>Previous</Button>
        <p role="status" className="text-xs text-muted-foreground">Page {cursors.length}</p>
        <Button variant="outline" className={actionClass} disabled={!notifications.data?.next_cursor || notifications.isFetching || notifications.isError} onClick={() => { if (notifications.data?.next_cursor) changePage([...cursors, notifications.data.next_cursor]) }}>Next</Button>
      </nav> : null}
    </div>
  )
}
