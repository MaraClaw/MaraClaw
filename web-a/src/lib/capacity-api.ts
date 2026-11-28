import { z } from 'zod'

import {
  CapacityDecisionRequestSchema, CapacityOrderCreateRequestSchema, CapacityOrderOutSchema, CapacityOrderPageSchema,
  CapacityOrderStatusValues, CapacityOverviewOutSchema, CapacityPaymentSettingsOutSchema, CapacityPaymentSettingsRequestSchema,
} from './engine-contracts.generated.ts'
import { MAX_CAPACITY_EXTRAS } from './capacity-pricing.ts'
import { ApiError, apiBinaryRequest, apiFormRequest, apiRequest, transferTimeoutMs } from './http.ts'

export const CAPACITY_ORDERS_PAGE_SIZE = 50
export const CAPACITY_RECENT_ORDERS_LIMIT = 10
export const CAPACITY_DECISION_NOTE_LIMIT = 1000
export const CAPACITY_EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
export const CAPACITY_SETTING_TEXT_LIMIT = 120
export const MAX_USD_VND_RATE = 1_000_000

export const capacityOrderIdSchema = z.uuid().brand<'CapacityOrderId'>()
export const capacityOrderStatusSchema = z.enum(CapacityOrderStatusValues)
export const capacityOrderSchema = CapacityOrderOutSchema.strip().extend({ id: capacityOrderIdSchema }).readonly()
export const capacityOverviewSchema = CapacityOverviewOutSchema.strip().extend({
  open_orders: z.array(capacityOrderSchema).readonly(), current_renewal: capacityOrderSchema.nullable(),
}).readonly()
const capacityOrderPageSchema = CapacityOrderPageSchema.strip().extend({
  items: z.array(capacityOrderSchema).readonly(),
}).readonly()
export const capacityPaymentSettingsSchema = CapacityPaymentSettingsOutSchema.strip().readonly()

const extraCountSchema = z.number().int().min(0).max(MAX_CAPACITY_EXTRAS)
export const capacityOrderRequestSchema = CapacityOrderCreateRequestSchema.extend({
  extra_accounts: extraCountSchema, extra_agents: extraCountSchema,
}).refine((order) => order.extra_accounts + order.extra_agents > 0, 'Order at least one extra account or agent.').readonly()
export const capacityDecisionSchema = CapacityDecisionRequestSchema.extend({
  note: z.string().trim().min(1).refine((note) => [...note].length <= CAPACITY_DECISION_NOTE_LIMIT).optional(),
}).refine((decision) => decision.action !== 'reject' || decision.note !== undefined, 'A rejection needs a note.').readonly()
export const capacityPaymentSettingsRequestSchema = CapacityPaymentSettingsRequestSchema.readonly()

export type CapacityOrderId = z.infer<typeof capacityOrderIdSchema>
export type CapacityOrderStatus = z.infer<typeof capacityOrderStatusSchema>
export type CapacityOrder = z.infer<typeof capacityOrderSchema>
export type CapacityOverview = z.infer<typeof capacityOverviewSchema>
export type CapacityOrderPage = z.infer<typeof capacityOrderPageSchema>
export type CapacityOrderRequest = z.infer<typeof capacityOrderRequestSchema>
export type CapacityDecision = z.infer<typeof capacityDecisionSchema>
export type CapacityPaymentSettings = z.infer<typeof capacityPaymentSettingsSchema>
export type CapacityPaymentSettingsRequest = z.infer<typeof capacityPaymentSettingsRequestSchema>

const conflictSchema = z.object({ code: z.string(), order_id: z.uuid().optional() })

/** The engine answers a refused purchase or transition with `409 {code, message, ...}`. */
export function capacityConflict(error: unknown): { readonly code: string; readonly orderId: CapacityOrderId | null } | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const parsed = conflictSchema.safeParse(error.detail)
  if (!parsed.success) return null
  return { code: parsed.data.code, orderId: parsed.data.order_id ? capacityOrderIdSchema.parse(parsed.data.order_id) : null }
}

export async function fetchCapacityOverview(tenantId: string | null, signal?: AbortSignal) {
  const query = new URLSearchParams()
  if (tenantId !== null) query.set('tenant_id', z.uuid().parse(tenantId))
  return apiRequest(`/api/capacity/overview?${query}`, { schema: capacityOverviewSchema, signal })
}

/** The server's `next_cursor`, opaque to the client: sent back exactly as received. */
const capacityCursorSchema = z.string().min(1)
const capacityPositionSchema = z.object({ cursor: capacityCursorSchema.nullable(), offset: z.int().nonnegative() })
  .refine((position) => position.cursor === null || position.offset === 0, 'A cursor cannot be combined with an offset')

export async function fetchCapacityOrders(
  scope: {
    readonly tenantId: string | null; readonly limit?: number; readonly offset?: number; readonly cursor?: string | null
  }, signal?: AbortSignal,
) {
  const query = new URLSearchParams({ limit: String(z.int().min(1).max(100).parse(scope.limit ?? CAPACITY_ORDERS_PAGE_SIZE)) })
  const position = capacityPositionSchema.parse({ cursor: scope.cursor ?? null, offset: scope.offset ?? 0 })
  if (position.cursor === null) query.set('offset', String(position.offset))
  else query.set('cursor', position.cursor)
  if (scope.tenantId !== null) query.set('tenant_id', z.uuid().parse(scope.tenantId))
  return apiRequest(`/api/capacity/orders?${query}`, { schema: capacityOrderPageSchema, signal })
}

export async function createCapacityOrder(order: CapacityOrderRequest) {
  return apiRequest('/api/capacity/orders', {
    method: 'POST', body: capacityOrderRequestSchema.parse(order), schema: capacityOrderSchema,
  })
}

export async function fetchCapacityOrder(id: CapacityOrderId, signal?: AbortSignal) {
  capacityOrderIdSchema.parse(id)
  return apiRequest(`/api/capacity/orders/${id}`, { schema: capacityOrderSchema, signal })
}

export async function cancelCapacityOrder(id: CapacityOrderId) {
  capacityOrderIdSchema.parse(id)
  return apiRequest(`/api/capacity/orders/${id}/cancel`, { method: 'POST', schema: capacityOrderSchema })
}

/** The "Finish" action: attaches the transfer evidence (multipart field `file`) and sends the order for review. */
export async function submitCapacityEvidence(id: CapacityOrderId, file: Blob, filename: string) {
  capacityOrderIdSchema.parse(id)
  const form = new FormData()
  form.append('file', file, filename)
  return capacityOrderSchema.parse(await apiFormRequest<unknown>(`/api/capacity/orders/${id}/evidence`, form, {
    timeoutMs: transferTimeoutMs(file.size),
  }))
}

/** Bearer-authenticated evidence bytes; the engine sniffs the type and sends it with `nosniff` and a CSP sandbox. */
export async function fetchCapacityEvidence(id: CapacityOrderId, signal?: AbortSignal) {
  capacityOrderIdSchema.parse(id)
  return apiBinaryRequest(`/api/capacity/orders/${id}/evidence`, { signal })
}

/** One page of the review queue for `status`: the first when `cursor` is null, else the page the server's cursor names. */
export async function fetchCapacityReviewOrders(status: CapacityOrderStatus, cursor: string | null = null, signal?: AbortSignal) {
  const query = new URLSearchParams({ status: capacityOrderStatusSchema.parse(status), limit: String(CAPACITY_ORDERS_PAGE_SIZE) })
  if (cursor !== null) query.set('cursor', capacityCursorSchema.parse(cursor))
  return apiRequest(`/api/admin/capacity-orders?${query}`, { schema: capacityOrderPageSchema, signal })
}

export async function fetchCapacityReviewOrder(id: CapacityOrderId, signal?: AbortSignal) {
  capacityOrderIdSchema.parse(id)
  return apiRequest(`/api/admin/capacity-orders/${id}`, { schema: capacityOrderSchema, signal })
}

export async function decideCapacityOrder(id: CapacityOrderId, decision: CapacityDecision) {
  capacityOrderIdSchema.parse(id)
  return apiRequest(`/api/admin/capacity-orders/${id}/decision`, {
    method: 'POST', body: capacityDecisionSchema.parse(decision), schema: capacityOrderSchema,
  })
}

export async function fetchCapacityPaymentSettings(signal?: AbortSignal) {
  return apiRequest('/api/admin/capacity-payment-settings', { schema: capacityPaymentSettingsSchema, signal })
}

export async function saveCapacityPaymentSettings(settings: CapacityPaymentSettingsRequest) {
  return apiRequest('/api/admin/capacity-payment-settings', {
    method: 'PUT', body: capacityPaymentSettingsRequestSchema.parse(settings), schema: capacityPaymentSettingsSchema,
  })
}
