import { z } from 'zod'

import { apiRequest } from './http.ts'

export const notificationSchema = z.object({
  id: z.uuid().brand<'NotificationId'>(),
  type: z.string(),
  title: z.string(),
  body: z.string(),
  link: z.string().nullable(),
  ref_id: z.string().nullable(),
  sender_name: z.string().nullable(),
  is_read: z.boolean(),
  created_at: z.iso.datetime({ offset: true, local: true }).nullable(),
}).readonly()

const notificationsSchema = z.array(notificationSchema).readonly()
const unreadCountSchema = z.object({ unread_count: z.number().int().nonnegative() }).readonly()
const readResultSchema = z.object({ ok: z.literal(true) }).readonly()

export type AdminNotification = z.infer<typeof notificationSchema>
export type NotificationId = AdminNotification['id']
export type NotificationUnreadCount = z.infer<typeof unreadCountSchema>
export const NOTIFICATIONS_PAGE_SIZE = 50

export async function fetchNotifications(offset = 0, signal?: AbortSignal) {
  return notificationsSchema.parse(await apiRequest<unknown>(
    `/api/notifications?limit=${NOTIFICATIONS_PAGE_SIZE}&offset=${offset}`, { signal },
  ))
}

export async function fetchNotificationUnreadCount(signal?: AbortSignal) {
  return unreadCountSchema.parse(await apiRequest<unknown>('/api/notifications/unread-count', { signal }))
}

export async function markNotificationRead(id: NotificationId) {
  return readResultSchema.parse(await apiRequest<unknown>(`/api/notifications/${id}/read`, { method: 'POST' }))
}

export async function markAllNotificationsRead() {
  return readResultSchema.parse(await apiRequest<unknown>('/api/notifications/read-all', { method: 'POST' }))
}
