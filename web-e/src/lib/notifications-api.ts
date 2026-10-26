import { z } from 'zod'

import { apiRequest } from './http.ts'
import { NotificationKindSchema, ReadSelectedRequestSchema } from './engine-contracts.generated.ts'
import type { NotificationKind as EngineNotificationKind } from './engine-contracts.generated.ts'

export const NotificationKind = {
  Mention: 'mention',
  PlazaReply: 'plaza_reply',
  PlazaComment: 'plaza_comment',
  Broadcast: 'broadcast',
  ApprovalPending: 'approval_pending',
  ApprovalResolved: 'approval_resolved',
  AutonomyL2: 'autonomy_l2',
  CompanyAdmissionPending: 'company_admission_pending',
  CompanyAdmissionResolved: 'company_admission_resolved',
  CompanyAdmissionCreated: 'company_admission_created',
  AgentDeletionRequested: 'agent_deletion_requested',
  AgentDeletionResolved: 'agent_deletion_resolved',
  AgentOwnershipTransferred: 'agent_ownership_transferred',
  OrganizationWelcome: 'organization_welcome',
  SystemAlert: 'system_alert',
  System: 'system',
  OpenClawUpdate: 'openclaw_update',
} as const satisfies Record<string, EngineNotificationKind>
export const notificationKindSchema = NotificationKindSchema
export type NotificationKind = EngineNotificationKind

export const notificationCategories = ['all', 'tool', 'approval', 'social', 'broadcast'] as const
export const notificationCategorySchema = z.enum(notificationCategories)
export type NotificationCategory = z.infer<typeof notificationCategorySchema>
export const notificationViews = ['active', 'archived'] as const
export const notificationViewSchema = z.enum(notificationViews)
export type NotificationView = z.infer<typeof notificationViewSchema>

const notificationSchema = z.object({
  id: z.string(),
  title: z.string(),
  body: z.string().nullish(),
  link: z.string().nullish(),
  type: notificationKindSchema,
  is_read: z.boolean().optional(),
  created_at: z.string().nullish(),
  archived_at: z.iso.datetime({ offset: true }).nullable(),
}).readonly()
export type NotificationRow = z.infer<typeof notificationSchema>

const countSchema = z.union([
  z.object({ unread_count: z.number().int().nonnegative() }).transform((value) => value.unread_count),
  z.object({ count: z.number().int().nonnegative() }).transform((value) => value.count),
])
const pageSchema = z.object({ limit: z.number().int().min(1).max(200), offset: z.number().int().nonnegative() })

export async function listNotifications({ category = 'all', view = 'active', limit = 50, offset = 0, signal }: {
  readonly category?: NotificationCategory
  readonly view?: NotificationView
  readonly limit?: number
  readonly offset?: number
  readonly signal?: AbortSignal
} = {}): Promise<readonly NotificationRow[]> {
  const page = pageSchema.parse({ limit, offset })
  const query = new URLSearchParams({ category, limit: String(page.limit), offset: String(page.offset), view: notificationViewSchema.parse(view) })
  return apiRequest(`/api/notifications?${query}`, {
    schema: z.array(notificationSchema), ...(signal === undefined ? {} : { signal }),
  })
}

export async function unreadNotificationCount(category: NotificationCategory = 'all', signal?: AbortSignal): Promise<number> {
  return apiRequest(`/api/notifications/unread-count?category=${category}`, {
    schema: countSchema, ...(signal === undefined ? {} : { signal }),
  })
}

export async function markNotificationRead(id: string): Promise<void> {
  await apiRequest(`/api/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' })
}

export async function markAllNotificationsRead(category: NotificationCategory): Promise<void> {
  await apiRequest(`/api/notifications/read-all?category=${category}`, { method: 'POST' })
}

const archiveResultSchema = z.object({ ok: z.literal(true) })

export async function markSelectedNotificationsRead(ids: readonly string[]): Promise<void> {
  const body = ReadSelectedRequestSchema.parse({ ids })
  await apiRequest('/api/notifications/read-selected', { method: 'POST', body, schema: archiveResultSchema })
}

export async function archiveNotification(id: string): Promise<void> {
  await apiRequest(`/api/notifications/${encodeURIComponent(id)}/archive`, { schema: archiveResultSchema, method: 'POST' })
}

export async function restoreNotification(id: string): Promise<void> {
  await apiRequest(`/api/notifications/${encodeURIComponent(id)}/restore`, { schema: archiveResultSchema, method: 'POST' })
}
