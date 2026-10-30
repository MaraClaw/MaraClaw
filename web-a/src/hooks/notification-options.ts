import { queryOptions, type QueryClient } from '@tanstack/react-query'

import {
  fetchNotifications, fetchNotificationUnreadCount, markAllNotificationsRead, markNotificationRead,
  archiveNotification, restoreNotification, markSelectedNotificationsRead, archiveSelectedNotifications, restoreSelectedNotifications,
  type NotificationPage, type NotificationId, type NotificationUnreadCount, type NotificationView,
} from '../lib/notifications-api.ts'

export const notificationKeys = {
  all: ['admin-notifications'] as const,
  lists: ['admin-notifications', 'list'] as const,
  view: (view: NotificationView) => ['admin-notifications', 'list', view] as const,
  list: (cursor: string | null, view: NotificationView = 'active') => ['admin-notifications', 'list', view, cursor] as const,
  unread: ['admin-notifications', 'unread-count'] as const,
}

export function notificationsOptions(cursor: string | null, enabled: boolean, view: NotificationView = 'active') {
  return queryOptions({
    queryKey: notificationKeys.list(cursor, view),
    queryFn: ({ signal }) => fetchNotifications(cursor, signal, view),
    enabled,
    refetchInterval: 30_000,
  })
}

export function notificationUnreadOptions(enabled: boolean) {
  return queryOptions({
    queryKey: notificationKeys.unread,
    queryFn: ({ signal }) => fetchNotificationUnreadCount(signal),
    enabled,
    refetchInterval: 30_000,
  })
}

export function notificationReadOptions(client: QueryClient, view: NotificationView = 'active') {
  return {
    mutationFn: markNotificationRead,
    scope: { id: 'admin-notifications-read' },
    onSuccess: (_result: unknown, id: NotificationId) => {
      void client.cancelQueries({ queryKey: notificationKeys.all })
      const wasUnread = view === 'active' && client.getQueriesData<NotificationPage>({ queryKey: notificationKeys.view('active') })
        .some(([, page]) => page?.items.some((item) => item.id === id && !item.is_read && item.archived_at === null))
      client.setQueriesData<NotificationPage>({ queryKey: notificationKeys.lists }, (page) =>
        page && { ...page, items: page.items.map((item) => item.id === id ? { ...item, is_read: true } : item) })
      if (wasUnread) {
        client.setQueryData<NotificationUnreadCount>(notificationKeys.unread, (count) =>
          count && { unread_count: Math.max(0, count.unread_count - 1) })
      }
      void client.invalidateQueries({ queryKey: notificationKeys.all })
    },
  }
}

export function notificationsReadAllOptions(client: QueryClient) {
  return {
    mutationFn: markAllNotificationsRead,
    scope: { id: 'admin-notifications-read' },
    onSuccess: () => {
      void client.cancelQueries({ queryKey: notificationKeys.all })
      client.setQueriesData<NotificationPage>({ queryKey: notificationKeys.view('active') }, (page) =>
        page && { ...page, items: page.items.map((item) => item.archived_at === null ? { ...item, is_read: true } : item) })
      client.setQueryData<NotificationUnreadCount>(notificationKeys.unread, { unread_count: 0 })
      void client.invalidateQueries({ queryKey: notificationKeys.all })
    },
  }
}

export function notificationArchiveOptions(client: QueryClient) {
  return {
    mutationFn: archiveNotification,
    scope: { id: 'admin-notifications-read' },
    onSuccess: () => {
      void client.cancelQueries({ queryKey: notificationKeys.all })
      return client.invalidateQueries({ queryKey: notificationKeys.all })
    },
  }
}

export function notificationRestoreOptions(client: QueryClient) {
  return {
    ...notificationArchiveOptions(client),
    mutationFn: restoreNotification,
  }
}

export function notificationsReadSelectedOptions(client: QueryClient) {
  return {
    ...notificationArchiveOptions(client),
    mutationFn: markSelectedNotificationsRead,
  }
}

export function notificationsArchiveSelectedOptions(client: QueryClient) {
  return { ...notificationArchiveOptions(client), mutationFn: archiveSelectedNotifications }
}

export function notificationsRestoreSelectedOptions(client: QueryClient) {
  return { ...notificationRestoreOptions(client), mutationFn: restoreSelectedNotifications }
}
