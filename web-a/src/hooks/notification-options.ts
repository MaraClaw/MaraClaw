import { queryOptions, type QueryClient } from '@tanstack/react-query'

import {
  fetchNotifications, fetchNotificationUnreadCount, markAllNotificationsRead, markNotificationRead,
  type AdminNotification, type NotificationId, type NotificationUnreadCount,
} from '../lib/notifications-api.ts'

export const notificationKeys = {
  all: ['admin-notifications'] as const,
  lists: ['admin-notifications', 'list'] as const,
  list: (offset: number) => ['admin-notifications', 'list', offset] as const,
  unread: ['admin-notifications', 'unread-count'] as const,
}

export function notificationsOptions(offset: number, enabled: boolean) {
  return queryOptions({
    queryKey: notificationKeys.list(offset),
    queryFn: ({ signal }) => fetchNotifications(offset, signal),
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

export function notificationReadOptions(client: QueryClient) {
  return {
    mutationFn: markNotificationRead,
    scope: { id: 'admin-notifications-read' },
    onSuccess: (_result: unknown, id: NotificationId) => {
      void client.cancelQueries({ queryKey: notificationKeys.all })
      const wasUnread = client.getQueriesData<readonly AdminNotification[]>({ queryKey: notificationKeys.lists })
        .some(([, notifications]) => notifications?.some((item) => item.id === id && !item.is_read))
      client.setQueriesData<readonly AdminNotification[]>({ queryKey: notificationKeys.lists }, (items) =>
        items?.map((item) => item.id === id ? { ...item, is_read: true } : item))
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
      client.setQueriesData<readonly AdminNotification[]>({ queryKey: notificationKeys.lists }, (items) =>
        items?.map((item) => ({ ...item, is_read: true })))
      client.setQueryData<NotificationUnreadCount>(notificationKeys.unread, { unread_count: 0 })
      void client.invalidateQueries({ queryKey: notificationKeys.all })
    },
  }
}
