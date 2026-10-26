import type { InfiniteData, QueryClient } from '@tanstack/react-query'

import {
  archiveNotification, listNotifications, markAllNotificationsRead, markNotificationRead, markSelectedNotificationsRead, restoreNotification, unreadNotificationCount,
  type NotificationCategory, type NotificationPage, type NotificationView,
} from '../lib/notifications-api.ts'

export const notificationKeys = {
  all: ['notifications'] as const,
  lists: ['notifications', 'list'] as const,
  list: (category: NotificationCategory, view: NotificationView = 'active', unreadOnly = false) => ['notifications', 'list', category, view, unreadOnly] as const,
  unread: (category: NotificationCategory) => ['notifications', 'unread', category] as const,
}

const pageSize = 50

export function notificationListOptions(category: NotificationCategory, view: NotificationView = 'active', unreadOnly = false) {
  return {
    queryKey: notificationKeys.list(category, view, unreadOnly),
    initialPageParam: null,
    queryFn: ({ pageParam, signal }: { pageParam: string | null; signal: AbortSignal }) => listNotifications({ category, view, limit: pageSize, cursor: pageParam, unreadOnly, signal }),
    getNextPageParam: (lastPage: NotificationPage) => lastPage.next_cursor ?? undefined,
  }
}

export function notificationUnreadOptions(category: NotificationCategory) {
  return {
    queryKey: notificationKeys.unread(category),
    queryFn: ({ signal }: { signal: AbortSignal }) => unreadNotificationCount(category, signal),
    refetchInterval: 30_000,
  }
}

function refreshNotifications(client: QueryClient) {
  // Cancellation happens synchronously before invalidation starts authoritative reads.
  // Refresh every category: broadcast rows also belong to social, and pages are incomplete.
  void client.cancelQueries({ queryKey: notificationKeys.all })
  client.setQueriesData<InfiniteData<NotificationPage, string | null>>({ queryKey: notificationKeys.lists }, (data) =>
    data && { pages: data.pages.slice(0, 1), pageParams: data.pageParams.slice(0, 1) })
  return client.invalidateQueries({ queryKey: notificationKeys.all, refetchType: 'all' })
}

export function notificationReadOptions(client: QueryClient) {
  return {
    scope: { id: 'notifications-read' },
    mutationFn: markNotificationRead,
    onSuccess: () => refreshNotifications(client),
  }
}

export function notificationsReadAllOptions(client: QueryClient) {
  return {
    scope: { id: 'notifications-read' },
    mutationFn: markAllNotificationsRead,
    onSuccess: () => refreshNotifications(client),
  }
}

export function notificationsReadSelectedOptions(client: QueryClient) {
  return {
    scope: { id: 'notifications-read' },
    mutationFn: markSelectedNotificationsRead,
    onSuccess: () => refreshNotifications(client),
  }
}

export function notificationArchiveOptions(client: QueryClient) {
  return {
    scope: { id: 'notifications-read' },
    mutationFn: archiveNotification,
    onSuccess: () => refreshNotifications(client),
  }
}

export function notificationRestoreOptions(client: QueryClient) {
  return {
    scope: { id: 'notifications-read' },
    mutationFn: restoreNotification,
    onSuccess: () => refreshNotifications(client),
  }
}
