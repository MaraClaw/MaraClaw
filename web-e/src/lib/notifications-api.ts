import { z } from 'zod'

import { apiRequest } from './http.ts'

export const notificationCategories = ['all', 'tool', 'approval', 'social', 'broadcast'] as const
export const notificationCategorySchema = z.enum(notificationCategories)
export type NotificationCategory = z.infer<typeof notificationCategorySchema>

const notificationSchema = z.object({
  id: z.string(),
  title: z.string(),
  body: z.string().nullish(),
  link: z.string().nullish(),
  type: z.string().optional(),
  is_read: z.boolean().optional(),
  created_at: z.string().nullish(),
}).readonly()
export type NotificationRow = z.infer<typeof notificationSchema>

const countSchema = z.union([
  z.object({ unread_count: z.number().int().nonnegative() }).transform((value) => value.unread_count),
  z.object({ count: z.number().int().nonnegative() }).transform((value) => value.count),
])
const pageSchema = z.object({ limit: z.number().int().min(1).max(200), offset: z.number().int().nonnegative() })

export async function listNotifications({ category = 'all', limit = 50, offset = 0, signal }: {
  readonly category?: NotificationCategory
  readonly limit?: number
  readonly offset?: number
  readonly signal?: AbortSignal
} = {}): Promise<readonly NotificationRow[]> {
  const page = pageSchema.parse({ limit, offset })
  const query = new URLSearchParams({ category, limit: String(page.limit), offset: String(page.offset) })
  const response = signal === undefined
    ? await apiRequest<unknown>(`/api/notifications?${query}`)
    : await apiRequest<unknown>(`/api/notifications?${query}`, { signal })
  return z.array(notificationSchema).parse(response)
}

export async function unreadNotificationCount(category: NotificationCategory = 'all', signal?: AbortSignal): Promise<number> {
  const response = signal === undefined
    ? await apiRequest<unknown>(`/api/notifications/unread-count?category=${category}`)
    : await apiRequest<unknown>(`/api/notifications/unread-count?category=${category}`, { signal })
  return countSchema.parse(response)
}

export async function markNotificationRead(id: string): Promise<void> {
  await apiRequest(`/api/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' })
}

export async function markAllNotificationsRead(category: NotificationCategory): Promise<void> {
  await apiRequest(`/api/notifications/read-all?category=${category}`, { method: 'POST' })
}
