import { useQuery, useQueryClient } from '@tanstack/react-query'

import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'
import type { NotificationView } from '@/lib/notifications-api'
import {
  notificationReadOptions, notificationsOptions, notificationsReadAllOptions, notificationUnreadOptions,
  notificationArchiveOptions, notificationRestoreOptions, notificationsReadSelectedOptions,
} from './notification-options'

export function useNotificationUnreadCount() {
  const { isAdmin, mustChangePassword } = useAuth()
  return useQuery(notificationUnreadOptions(isAdmin && !mustChangePassword))
}

export function useNotifications(offset: number, view: NotificationView = 'active') {
  const { isAdmin, mustChangePassword } = useAuth()
  const client = useQueryClient()
  const notifications = useQuery(notificationsOptions(offset, isAdmin && !mustChangePassword, view))
  const markRead = useSessionMutation(notificationReadOptions(client, view))
  const markAllRead = useSessionMutation(notificationsReadAllOptions(client))
  const markSelectedRead = useSessionMutation(notificationsReadSelectedOptions(client))
  const archive = useSessionMutation(notificationArchiveOptions(client))
  const restore = useSessionMutation(notificationRestoreOptions(client))
  return { notifications, markRead, markAllRead, markSelectedRead, archive, restore }
}
