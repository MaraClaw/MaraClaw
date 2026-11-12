import { useTranslation } from '@/hooks/use-translation'
import { useImperativeHandle, useMemo, useRef, useState, type RefObject } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { AdmissionInboxGate } from '@/components/layout/admission-access-gate'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { notificationArchiveOptions, notificationKeys, notificationListOptions, notificationReadOptions, notificationRestoreOptions, notificationsArchiveSelectedOptions, notificationsReadAllOptions, notificationsReadSelectedOptions, notificationsUnreadSelectedOptions, notificationsRestoreSelectedOptions, notificationUnreadOptions } from '@/hooks/notification-options'
import { useNotificationSelection } from '@/hooks/use-notification-selection'
import { useInfiniteQuery, useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { notificationCategories, notificationCategorySchema, notificationViews, type NotificationCategory, type NotificationView } from '@/lib/notifications-api'
import { relativeTime } from '@/lib/relative-time'
import { cn } from '@/lib/utils'
import { AdmissionsPage } from './admissions'

const actionClass = 'h-auto min-h-11 whitespace-normal'
const categoryLabels = { all: 'All', tool: 'Tools', approval: 'Approvals', social: 'Social', broadcast: 'Broadcasts' } satisfies Record<NotificationCategory, string>
const viewLabels = { active: 'Active', archived: 'Archived' } satisfies Record<NotificationView, string>

function memberLink(link?: string | null): string | null {
  if (!link) return null
  if (link.startsWith('/clawbook')) return `/app${link}`
  return link
}

export function NotificationsPage() {
  const { t: translate, owned } = useTranslation()
  const [search, setSearch] = useSearchParams()
  const requested = search.get('category')
  const parsed = notificationCategorySchema.safeParse(requested ?? 'all')
  const category = requested === 'admissions' ? 'admissions' : parsed.success ? parsed.data : 'all'
  const [view, setView] = useState<NotificationView>('active')
  const heading = useRef<HTMLHeadingElement>(null)
  const resetErrors = useRef<(() => void) | null>(null)
  const scope = useWorkspaceSession()

  return <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
    <h1 ref={heading} tabIndex={-1} className="font-display text-2xl font-semibold focus-visible:outline focus-visible:outline-ring">{translate("Inbox")}</h1>
    <div className="space-y-2">
      <Label htmlFor="notification-category">{translate("Category")}</Label>
      <Select id="notification-category" value={category} onChange={(event) => scope.effect(() => {
        const value = event.currentTarget.value
        const next = value === 'admissions' ? value : notificationCategorySchema.parse(value)
        if (next === category) return
        resetErrors.current?.()
        if (next !== 'admissions') void scope.client.resetQueries({ queryKey: notificationKeys.list(next, view), exact: true })
        setSearch((current) => { const params = new URLSearchParams(current); params.set('category', next); return params })
      })()}>
        {notificationCategories.map((value) => <option key={value} value={value}>{owned(categoryLabels[value])}</option>)}
        <option value="admissions">{translate("Admissions")}</option>
      </Select>
    </div>
    {category === 'admissions' ? <AdmissionsPage /> : <AdmissionInboxGate><NotificationContent category={category} view={view} setView={setView} heading={heading} resetErrorsRef={resetErrors} /></AdmissionInboxGate>}
  </div>
}

function NotificationContent({ category, view, setView, heading, resetErrorsRef }: {
  readonly category: NotificationCategory
  readonly view: NotificationView
  readonly setView: (view: NotificationView) => void
  readonly heading: RefObject<HTMLHeadingElement | null>
  readonly resetErrorsRef: RefObject<(() => void) | null>
}) {
  const { t: translate, owned, formatDate, formatNumber, locale } = useTranslation()
  const scope = useWorkspaceSession()
  const query = useInfiniteQuery(notificationListOptions(category, view))
  const unreadQuery = useQuery(notificationUnreadOptions(category))
  const markRead = useMutation(notificationReadOptions(scope.client))
  const markAll = useMutation(notificationsReadAllOptions(scope.client))
  const markSelected = useMutation(notificationsReadSelectedOptions(scope.client))
  const markSelectedUnread = useMutation(notificationsUnreadSelectedOptions(scope.client))
  const archive = useMutation(notificationArchiveOptions(scope.client))
  const restore = useMutation(notificationRestoreOptions(scope.client))
  const archiveSelected = useMutation(notificationsArchiveSelectedOptions(scope.client))
  const restoreSelected = useMutation(notificationsRestoreSelectedOptions(scope.client))
  const moveSelected = view === 'active' ? archiveSelected : restoreSelected
  const pending = markRead.isPending || markAll.isPending || markSelected.isPending || markSelectedUnread.isPending || archive.isPending || restore.isPending || archiveSelected.isPending || restoreSelected.isPending
  const hasUnread = (unreadQuery.data ?? 0) > 0
  const items = [...new Map(query.data?.pages.flatMap((page) => page.items).map((item) => [item.id, item])).values()]
  const selectionContext = useMemo(() => ({ scope, category, view }), [scope, category, view])
  const selection = useNotificationSelection(items, selectionContext)

  function resetErrors() {
    if (!markRead.isPending) markRead.reset()
    if (!markAll.isPending) markAll.reset()
    if (!markSelected.isPending) markSelected.reset()
    if (!markSelectedUnread.isPending) markSelectedUnread.reset()
    if (!archive.isPending) archive.reset()
    if (!restore.isPending) restore.reset()
    if (!archiveSelected.isPending) archiveSelected.reset()
    if (!restoreSelected.isPending) restoreSelected.reset()
  }
  useImperativeHandle(resetErrorsRef, () => resetErrors)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {view === 'active' ? <Button variant="outline" className={actionClass} aria-describedby="notification-bulk-scope" disabled={pending || !hasUnread} onClick={() => { resetErrors(); markAll.mutate(category) }}>
          {markAll.isPending ? translate("Marking all read…") : translate("Mark all read")}
        </Button> : null}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button className={cn(actionClass, 'disabled:bg-secondary disabled:text-secondary-foreground disabled:opacity-100 disabled:shadow-none')} disabled={pending || selection.ids.length === 0} onClick={() => {
          if (pending || selection.ids.length === 0) return
          resetErrors()
          markSelected.mutate([...selection.ids], { onSuccess: scope.effect(() => { if (selection.complete()) heading.current?.focus() }) })
        }}>{markSelected.isPending ? translate("Marking selected read…") : translate("Mark selected read")}</Button>
        <Button variant="outline" className={actionClass} disabled={pending || selection.ids.length === 0} onClick={() => {
          if (pending || selection.ids.length === 0) return
          resetErrors()
          markSelectedUnread.mutate([...selection.ids], { onSuccess: scope.effect(() => { if (selection.complete()) heading.current?.focus() }) })
        }}>{markSelectedUnread.isPending ? translate("Marking selected unread…") : translate("Mark selected unread")}</Button>
        <Button variant="outline" className={actionClass} disabled={pending || selection.ids.length === 0} onClick={() => {
          if (pending || selection.ids.length === 0) return
          resetErrors()
          moveSelected.mutate([...selection.ids], { onSuccess: scope.effect(() => { if (selection.complete()) heading.current?.focus() }) })
        }}>{view === 'active' ? archiveSelected.isPending ? translate("Archiving selected…") : translate("Archive selected") : restoreSelected.isPending ? translate("Restoring selected…") : translate("Restore selected")}</Button>
        <p role="status" className="text-sm text-muted-foreground">{formatNumber(selection.ids.length)} {translate("selected (up to 200 loaded notifications)")}</p>
        {selection.ids.length > 0 ? <Button variant="ghost" className={actionClass} onClick={selection.clear}>{translate("Clear selection")}</Button> : null}
      </div>
      <fieldset className="flex flex-wrap gap-2">
        <legend className="sr-only">{translate("Inbox view")}</legend>
        {notificationViews.map((value) => <Button key={value} variant={view === value ? 'secondary' : 'outline'} className={actionClass} aria-pressed={view === value} onClick={scope.effect(() => {
          if (view === value) return
          resetErrors()
          void scope.client.resetQueries({ queryKey: notificationKeys.list(category, value), exact: true })
          setView(value)
        })}>{owned(viewLabels[value])}</Button>)}
      </fieldset>
      <div className="space-y-2">
        <p id="notification-bulk-scope" className="text-sm text-muted-foreground">{view === 'active' ? translate("Mark all read covers ALL active notifications matching this category, including those not loaded.") : translate("Archived notifications keep their read status and do not count toward unread totals. Restore them to return them to Active.")}</p>
        <p role="status" className="text-sm text-muted-foreground">{unreadQuery.data === undefined ? translate("Checking active unread notifications…") : translate("{count} unread in {category}", { count: formatNumber(unreadQuery.data), category: owned(categoryLabels[category]) })}</p>
        {unreadQuery.isError ? <div className="space-y-2">
          <RequestErrorToast error={unreadQuery.error} message="Unable to check unread notifications." />
          <Button variant="outline" className={actionClass} disabled={unreadQuery.isFetching} onClick={scope.effect(() => void unreadQuery.refetch())}>{translate("Retry unread count")}</Button>
        </div> : null}
      </div>
      <p role="status" aria-atomic="true" className="sr-only">{archiveSelected.isSuccess ? translate("Selected notifications archived. Their read status is unchanged.") : restoreSelected.isSuccess ? translate("Selected notifications restored to Active. Their read status is unchanged.") : archive.isSuccess ? translate("Notification archived. Its read status is unchanged.") : restore.isSuccess ? translate("Notification restored to Active.") : markRead.isSuccess ? translate("Notification marked as read.") : markSelected.isSuccess ? translate("Selected notifications marked as read.") : markSelectedUnread.isSuccess ? translate("Selected notifications marked as unread.") : markAll.isSuccess ? translate("All active notifications in {category} marked as read.", { category: owned(categoryLabels[markAll.variables]) }) : ''}</p>
      {markSelectedUnread.isError ? <RequestErrorToast error={markSelectedUnread.error} message={userFacingRequestError(markSelectedUnread.error, translate("Unable to mark selected notifications as unread. Your selection is kept. Try again."))} /> : null}
      {markRead.isError || markAll.isError || markSelected.isError ? <RequestErrorToast error={markRead.error ?? markAll.error ?? markSelected.error} message={userFacingRequestError(markRead.error ?? markAll.error ?? markSelected.error, translate("Unable to mark notifications as read. Try again."))} /> : null}
      {archive.isError || restore.isError || archiveSelected.isError || restoreSelected.isError ? <RequestErrorToast error={archive.error ?? restore.error ?? archiveSelected.error ?? restoreSelected.error} message={userFacingRequestError(archive.error ?? restore.error ?? archiveSelected.error ?? restoreSelected.error, archiveSelected.isError || restoreSelected.isError ? translate("Unable to move selected notifications. Your selection is kept. Try again.") : translate("Unable to move this notification. Try again."))} /> : null}
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading notifications…")}</p> : null}
      {query.isError && !query.isFetchNextPageError ? <div className="space-y-3 rounded-xl border border-border p-4">
        <RequestErrorToast error={query.error} message={userFacingRequestError(query.error, translate("Unable to load notifications. Check your connection and try again."))} />
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={() => void query.refetch()}>{translate("Retry")}</Button>
      </div> : null}
      <ul aria-label={translate("Notifications")} className="space-y-2">
        {items.map((item) => {
          const link = memberLink(item.link)
          return <li key={item.id} className={cn('space-y-3 rounded-xl border border-border px-3 py-3', item.is_read ? 'bg-card' : 'bg-accent')}>
            <div className="space-y-1">
              <label className="flex min-h-11 w-fit items-center gap-3 text-sm text-muted-foreground">
                <input type="checkbox" className="size-5 accent-primary" aria-label={translate("Select notification: {title}", { title: item.title })} checked={selection.ids.includes(item.id)} disabled={selection.ids.length >= 200 && !selection.ids.includes(item.id)} onChange={() => selection.toggle(item.id)} />
                {translate("Select notification")}</label>
              <p className="text-sm font-medium">{item.title}</p>
              {item.body ? <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{item.body}</p> : null}
              {item.created_at ? <time className="block text-xs text-muted-foreground" dateTime={item.created_at} title={formatDate(item.created_at, { dateStyle: 'medium', timeStyle: 'short' })}>{relativeTime(item.created_at, undefined, locale)}</time> : null}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {link ? <Button asChild variant="outline" className={actionClass}><Link to={link} aria-label={translate("Open notification: {title}", { title: item.title })}>{translate("Open notification")}</Link></Button> : null}
              {!item.is_read ? <Button variant="ghost" className={actionClass} disabled={pending} aria-label={translate("Mark as read: {title}", { title: item.title })} onClick={() => { resetErrors(); markRead.mutate(item.id) }}>
                {markRead.isPending && markRead.variables === item.id ? translate("Marking as read…") : translate("Mark as read")}
              </Button> : <span className="text-xs text-muted-foreground">{translate("Read")}</span>}
              {view === 'active' ? <Button variant="outline" className={actionClass} disabled={pending} aria-label={translate("Archive notification: {title}", { title: item.title })} onClick={() => { resetErrors(); archive.mutate(item.id) }}>
                {archive.isPending && archive.variables === item.id ? translate("Archiving…") : translate("Archive")}
              </Button> : <Button variant="outline" className={actionClass} disabled={pending} aria-label={translate("Restore notification: {title}", { title: item.title })} onClick={() => { resetErrors(); restore.mutate(item.id) }}>
                {restore.isPending && restore.variables === item.id ? translate("Restoring…") : translate("Restore")}
              </Button>}
            </div>
          </li>
        })}
      </ul>
      {query.isFetchNextPageError ? <RequestErrorToast error={query.error} message="Unable to load more notifications. Try again." /> : null}
      {query.hasNextPage ? <Button variant="outline" className={actionClass} disabled={query.isFetching || pending} onClick={() => {
        const loadedPages = query.data?.pages.length ?? 0
        void query.fetchNextPage()?.then(scope.effect((result) => { if (result.isSuccess && (result.data?.pages.length ?? 0) > loadedPages) selection.loadedMore() }))
      }}>
        {query.isFetchingNextPage ? translate("Loading more…") : query.isFetchNextPageError ? translate("Retry load more") : translate("Load more")}
      </Button> : null}
      {query.isSuccess && items.length === 0 ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p className="text-sm text-muted-foreground">{view === 'active' ? translate("No active notifications yet. Refresh to check for new updates.") : translate("No archived notifications in this category. Archive a notification from Active to keep it here.")}</p>
        <Button variant="outline" className={actionClass} disabled={query.isFetching} onClick={scope.effect(() => { selection.clear(); void scope.client.resetQueries({ queryKey: notificationKeys.list(category, view), exact: true }) })}>{translate("Refresh Inbox")}</Button>
      </div> : null}
    </div>
  )
}
