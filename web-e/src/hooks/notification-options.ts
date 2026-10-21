import type { QueryClient } from '@tanstack/react-query'

import { markAllNotificationsRead, markNotificationRead, type NotificationRow } from '../lib/workspace-api.ts'

type UnreadCount = Awaited<ReturnType<typeof import('../lib/workspace-api.ts').unreadNotificationCount>>

export function notificationReadOptions(client: QueryClient) {
  return {
    mutationFn: markNotificationRead,
    scope: { id: 'notifications-read' },
    onSuccess: (_result: unknown, id: string) => {
      void client.cancelQueries({ queryKey: ['notifications'] })
      const wasUnread = client.getQueryData<readonly NotificationRow[]>(['notifications'])
        ?.some((item) => item.id === id && !item.is_read)
      client.setQueryData<readonly NotificationRow[]>(['notifications'], (items) =>
        items?.map((item) => item.id === id ? { ...item, is_read: true } : item))
      if (wasUnread) {
        client.setQueryData<UnreadCount>(['notifications', 'unread'], (count) => count && {
          ...count,
          unread_count: Math.max(0, (count.unread_count ?? count.count ?? 0) - 1),
        })
      }
      void client.invalidateQueries({ queryKey: ['notifications'] })
    },
  }
}

export function notificationsReadAllOptions(client: QueryClient) {
  return {
    mutationFn: markAllNotificationsRead,
    scope: { id: 'notifications-read' },
    onSuccess: () => {
      void client.cancelQueries({ queryKey: ['notifications'] })
      client.setQueryData<readonly NotificationRow[]>(['notifications'], (items) =>
        items?.map((item) => ({ ...item, is_read: true })))
      client.setQueryData<UnreadCount>(['notifications', 'unread'], { unread_count: 0 })
      void client.invalidateQueries({ queryKey: ['notifications'] })
    },
  }
}
