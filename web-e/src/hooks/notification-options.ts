import type { QueryClient } from '@tanstack/react-query'

import {
  listNotifications, markAllNotificationsRead, markNotificationRead, unreadNotificationCount,
  type NotificationCategory, type NotificationRow,
} from '../lib/notifications-api.ts'

export const notificationKeys = {
  all: ['notifications'] as const,
  list: (category: NotificationCategory) => ['notifications', 'list', category] as const,
  unread: (category: NotificationCategory) => ['notifications', 'unread', category] as const,
}

const pageSize = 50

export function notificationListOptions(category: NotificationCategory) {
  return {
    queryKey: notificationKeys.list(category),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }: { pageParam: number; signal: AbortSignal }) => listNotifications({ category, limit: pageSize, offset: pageParam, signal }),
    getNextPageParam: (lastPage: readonly NotificationRow[], _pages: readonly (readonly NotificationRow[])[], lastOffset: number) => lastPage.length === pageSize ? lastOffset + pageSize : undefined,
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
