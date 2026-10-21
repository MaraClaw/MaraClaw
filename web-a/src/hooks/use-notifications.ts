import { useQuery, useQueryClient } from '@tanstack/react-query'

import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'
import {
  notificationReadOptions, notificationsOptions, notificationsReadAllOptions, notificationUnreadOptions,
} from './notification-options'

export function useNotificationUnreadCount() {
  const { isAdmin, mustChangePassword } = useAuth()
  return useQuery(notificationUnreadOptions(isAdmin && !mustChangePassword))
}

export function useNotifications(offset: number) {
  const { isAdmin, mustChangePassword } = useAuth()
  const client = useQueryClient()
  const notifications = useQuery(notificationsOptions(offset, isAdmin && !mustChangePassword))
  const markRead = useSessionMutation(notificationReadOptions(client))
  const markAllRead = useSessionMutation(notificationsReadAllOptions(client))
  return { notifications, markRead, markAllRead }
}
