import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { notificationArchiveOptions, notificationKeys, notificationListOptions, notificationReadOptions, notificationRestoreOptions, notificationsReadAllOptions, notificationsReadSelectedOptions, notificationUnreadOptions } from '@/hooks/notification-options'
import { useNotificationSelection } from '@/hooks/use-notification-selection'
import { useInfiniteQuery, useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { notificationCategories, notificationCategorySchema, notificationViews, type NotificationCategory, type NotificationView } from '@/lib/notifications-api'
import { relativeTime } from '@/lib/relative-time'
import { cn } from '@/lib/utils'

const actionClass = 'h-auto min-h-11 whitespace-normal'
const categoryLabels = { all: 'All', tool: 'Tools', approval: 'Approvals', social: 'Social', broadcast: 'Broadcasts' } satisfies Record<NotificationCategory, string>
const viewLabels = { active: 'Active', archived: 'Archived' } satisfies Record<NotificationView, string>

function memberLink(link?: string | null): string | null {
  if (!link) return null
  if (link.startsWith('/plaza')) return `/app${link}`
  return link
}

export function NotificationsPage() {
  const [category, setCategory] = useState<NotificationCategory>('all')
  const [view, setView] = useState<NotificationView>('active')
  const scope = useWorkspaceSession()
  const query = useInfiniteQuery(notificationListOptions(category, view))
  const unreadQuery = useQuery(notificationUnreadOptions(category))
  const markRead = useMutation(notificationReadOptions(scope.client))
  const markAll = useMutation(notificationsReadAllOptions(scope.client))
  const markSelected = useMutation(notificationsReadSelectedOptions(scope.client))
  const archive = useMutation(notificationArchiveOptions(scope.client))
  const restore = useMutation(notificationRestoreOptions(scope.client))
  const pending = markRead.isPending || markAll.isPending || markSelected.isPending || archive.isPending || restore.isPending
  const hasUnread = (unreadQuery.data ?? 0) > 0
  const items = [...new Map(query.data?.pages.flatMap((page) => page.items).map((item) => [item.id, item])).values()]
  const selectionContext = useMemo(() => ({ scope, category, view }), [scope, category, view])
  const selection = useNotificationSelection(items, selectionContext)

  function resetErrors() {
    if (!markRead.isPending) markRead.reset()
    if (!markAll.isPending) markAll.reset()
    if (!markSelected.isPending) markSelected.reset()
    if (!archive.isPending) archive.reset()
    if (!restore.isPending) restore.reset()
  }

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">Inbox</h1>
        {view === 'active' ? <Button variant="outline" className={actionClass} aria-describedby="notification-bulk-scope" disabled={pending || !hasUnread} onClick={() => { resetErrors(); markAll.mutate(category) }}>
          {markAll.isPending ? 'Marking all read…' : 'Mark all read'}
        </Button> : null}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button className={cn(actionClass, 'disabled:bg-secondary disabled:text-secondary-foreground disabled:opacity-100 disabled:shadow-none')} disabled={pending || selection.ids.length === 0} onClick={() => {
          if (pending || selection.ids.length === 0) return
          resetErrors()
          markSelected.mutate([...selection.ids], { onSuccess: scope.effect(selection.complete) })
        }}>{markSelected.isPending ? 'Marking selected read…' : 'Mark selected read'}</Button>
        <p role="status" className="text-sm text-muted-foreground">{selection.ids.length} selected (up to 200 loaded notifications)</p>
        {selection.ids.length > 0 ? <Button variant="ghost" className={actionClass} onClick={selection.clear}>Clear selection</Button> : null}
      </div>
      <fieldset className="flex flex-wrap gap-2">
        <legend className="sr-only">Inbox view</legend>
        {notificationViews.map((value) => <Button key={value} variant={view === value ? 'secondary' : 'outline'} className={actionClass} aria-pressed={view === value} onClick={scope.effect(() => {
          if (view === value) return
          resetErrors()
          void scope.client.resetQueries({ queryKey: notificationKeys.list(category, value), exact: true })
          setView(value)
        })}>{viewLabels[value]}</Button>)}
      </fieldset>
      <div className="space-y-2">
        <Label htmlFor="notification-category">Category</Label>
        <Select id="notification-category" value={category} onChange={(event) => {
          const next = notificationCategorySchema.parse(event.currentTarget.value)
          resetErrors()
          void scope.client.resetQueries({ queryKey: notificationKeys.list(next, view), exact: true })
          setCategory(next)
        }}>
          {notificationCategories.map((value) => <option key={value} value={value}>{categoryLabels[value]}</option>)}
        </Select>
        <p id="notification-bulk-scope" className="text-sm text-muted-foreground">{view === 'active' ? 'Mark all read covers ALL active notifications matching this category, including those not loaded.' : 'Archived notifications keep their read status and do not count toward unread totals. Restore them to return them to Active.'}</p>
        <p role="status" className="text-sm text-muted-foreground">{unreadQuery.data === undefined ? 'Checking active unread notifications…' : `${unreadQuery.data} unread in ${categoryLabels[category]}`}</p>
        {unreadQuery.isError ? <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">Unable to check unread notifications.</p>
          <Button variant="outline" className={actionClass} disabled={unreadQuery.isFetching} onClick={scope.effect(() => void unreadQuery.refetch())}>Retry unread count</Button>
        </div> : null}
      </div>
      <p role="status" aria-atomic="true" className="sr-only">{archive.isSuccess ? 'Notification archived. Its read status is unchanged.' : restore.isSuccess ? 'Notification restored to Active.' : markRead.isSuccess ? 'Notification marked as read.' : markSelected.isSuccess ? 'Selected notifications marked as read.' : markAll.isSuccess ? `All active notifications in ${categoryLabels[markAll.variables]} marked as read.` : ''}</p>
      {markRead.isError || markAll.isError || markSelected.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(markRead.error ?? markAll.error ?? markSelected.error, 'Unable to mark notifications as read. Try again.')}</p> : null}
      {archive.isError || restore.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(archive.error ?? restore.error, 'Unable to move this notification. Try again.')}</p> : null}
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading notifications…</p> : null}
      {query.isError && !query.isFetchNextPageError ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load notifications. Check your connection and try again.')}</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.refetch()}>Retry</Button>
      </div> : null}
      <ul aria-label="Notifications" className="space-y-2">
        {items.map((item) => {
          const link = memberLink(item.link)
          return <li key={item.id} className={cn('space-y-3 rounded-xl border border-border px-3 py-3', item.is_read ? 'bg-card' : 'bg-accent')}>
            <div className="space-y-1">
              <label className="flex min-h-11 w-fit items-center gap-3 text-sm text-muted-foreground">
                <input type="checkbox" className="size-5 accent-primary" aria-label={`Select notification: ${item.title}`} checked={selection.ids.includes(item.id)} disabled={selection.ids.length >= 200 && !selection.ids.includes(item.id)} onChange={() => selection.toggle(item.id)} />
                Select notification
              </label>
              <p className="text-sm font-medium">{item.title}</p>
              {item.body ? <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{item.body}</p> : null}
              {item.created_at ? <time className="block text-xs text-muted-foreground" dateTime={item.created_at} title={new Date(item.created_at).toLocaleString()}>{relativeTime(item.created_at)}</time> : null}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {link ? <Button asChild variant="outline" className={actionClass}><Link to={link} aria-label={`Open notification: ${item.title}`}>Open notification</Link></Button> : null}
              {!item.is_read ? <Button variant="ghost" className={actionClass} disabled={pending} aria-label={`Mark as read: ${item.title}`} onClick={() => { resetErrors(); markRead.mutate(item.id) }}>
                {markRead.isPending && markRead.variables === item.id ? 'Marking as read…' : 'Mark as read'}
              </Button> : <span className="text-xs text-muted-foreground">Read</span>}
              {view === 'active' ? <Button variant="outline" className={actionClass} disabled={pending} aria-label={`Archive notification: ${item.title}`} onClick={() => { resetErrors(); archive.mutate(item.id) }}>
                {archive.isPending && archive.variables === item.id ? 'Archiving…' : 'Archive'}
              </Button> : <Button variant="outline" className={actionClass} disabled={pending} aria-label={`Restore notification: ${item.title}`} onClick={() => { resetErrors(); restore.mutate(item.id) }}>
                {restore.isPending && restore.variables === item.id ? 'Restoring…' : 'Restore'}
              </Button>}
            </div>
          </li>
        })}
      </ul>
      {query.isFetchNextPageError ? <p role="alert" className="text-sm text-destructive">Unable to load more notifications. Try again.</p> : null}
      {query.hasNextPage ? <Button variant="outline" className={actionClass} disabled={query.isFetching || pending} onClick={() => {
        const loadedPages = query.data?.pages.length ?? 0
        void query.fetchNextPage()?.then(scope.effect((result) => { if (result.isSuccess && (result.data?.pages.length ?? 0) > loadedPages) selection.loadedMore() }))
      }}>
        {query.isFetchingNextPage ? 'Loading more…' : query.isFetchNextPageError ? 'Retry load more' : 'Load more'}
      </Button> : null}
      {query.isSuccess && items.length === 0 ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p className="text-sm text-muted-foreground">{view === 'active' ? 'No active notifications yet. Refresh to check for new updates.' : 'No archived notifications in this category. Archive a notification from Active to keep it here.'}</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={scope.effect(() => { selection.clear(); void scope.client.resetQueries({ queryKey: notificationKeys.list(category, view), exact: true }) })}>Refresh Inbox</Button>
      </div> : null}
    </div>
  )
}
