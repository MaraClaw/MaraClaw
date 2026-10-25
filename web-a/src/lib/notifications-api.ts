import { z } from 'zod'

import { apiRequest } from './http.ts'
import {
  NotificationKind as EngineNotificationKind, NotificationKindSchema, NotificationReadResponseSchema,
  NotificationResponseSchema, NotificationUnreadCountResponseSchema,
} from './engine-contracts.generated.ts'

export const NotificationKind = {
  Mention: EngineNotificationKind.MENTION,
  PlazaReply: EngineNotificationKind.PLAZA_REPLY,
  PlazaComment: EngineNotificationKind.PLAZA_COMMENT,
  Broadcast: EngineNotificationKind.BROADCAST,
  ApprovalPending: EngineNotificationKind.APPROVAL_PENDING,
  ApprovalResolved: EngineNotificationKind.APPROVAL_RESOLVED,
  AutonomyL2: EngineNotificationKind.AUTONOMY_L2,
  CompanyAdmissionPending: EngineNotificationKind.COMPANY_ADMISSION_PENDING,
  CompanyAdmissionResolved: EngineNotificationKind.COMPANY_ADMISSION_RESOLVED,
  CompanyAdmissionCreated: EngineNotificationKind.COMPANY_ADMISSION_CREATED,
  AgentDeletionRequested: EngineNotificationKind.AGENT_DELETION_REQUESTED,
  AgentDeletionResolved: EngineNotificationKind.AGENT_DELETION_RESOLVED,
  AgentOwnershipTransferred: EngineNotificationKind.AGENT_OWNERSHIP_TRANSFERRED,
  OrganizationWelcome: EngineNotificationKind.ORGANIZATION_WELCOME,
  SystemAlert: EngineNotificationKind.SYSTEM_ALERT,
  System: EngineNotificationKind.SYSTEM,
  OpenClawUpdate: EngineNotificationKind.OPENCLAW_UPDATE,
} as const
export const notificationKindSchema = NotificationKindSchema
export type NotificationKind = z.infer<typeof notificationKindSchema>

export const notificationSchema = NotificationResponseSchema.strip().extend({
  id: z.uuid().brand<'NotificationId'>(),
  archived_at: z.iso.datetime({ offset: true, local: true }).nullable(),
  created_at: z.iso.datetime({ offset: true, local: true }).nullable(),
}).readonly()

const notificationsSchema = z.array(notificationSchema).readonly()
const unreadCountSchema = NotificationUnreadCountResponseSchema.strip().extend({ unread_count: z.number().int().nonnegative() }).readonly()
const readResultSchema = NotificationReadResponseSchema.strip().extend({ ok: z.literal(true) }).readonly()

export type AdminNotification = z.infer<typeof notificationSchema>
export type NotificationId = AdminNotification['id']
export type NotificationUnreadCount = z.infer<typeof unreadCountSchema>
export type NotificationView = 'active' | 'archived'
export const NOTIFICATIONS_PAGE_SIZE = 50

export async function fetchNotifications(offset = 0, signal?: AbortSignal, view: NotificationView = 'active') {
  return apiRequest(
    `/api/notifications?limit=${NOTIFICATIONS_PAGE_SIZE}&offset=${offset}&view=${view}`, { schema: notificationsSchema, signal },
  )
}

export async function fetchNotificationUnreadCount(signal?: AbortSignal) {
  return apiRequest('/api/notifications/unread-count', { schema: unreadCountSchema, signal })
}

export async function markNotificationRead(id: NotificationId) {
  return apiRequest(`/api/notifications/${id}/read`, { schema: readResultSchema, method: 'POST' })
}

export async function markAllNotificationsRead() {
  return apiRequest('/api/notifications/read-all', { schema: readResultSchema, method: 'POST' })
}

export async function archiveNotification(id: NotificationId) {
  return apiRequest(`/api/notifications/${id}/archive`, { schema: readResultSchema, method: 'POST' })
}

export async function restoreNotification(id: NotificationId) {
  return apiRequest(`/api/notifications/${id}/restore`, { schema: readResultSchema, method: 'POST' })
}
