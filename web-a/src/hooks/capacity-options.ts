import { queryOptions, skipToken, type QueryClient } from '@tanstack/react-query'

import {
  cancelCapacityOrder, createCapacityOrder, decideCapacityOrder, fetchCapacityOrder, fetchCapacityOrders,
  fetchCapacityOverview, fetchCapacityPaymentSettings, fetchCapacityReviewOrder, fetchCapacityReviewOrders,
  saveCapacityPaymentSettings, submitCapacityEvidence,
  type CapacityDecision, type CapacityOrder, type CapacityOrderId, type CapacityOrderRequest, type CapacityOrderStatus,
  type CapacityPaymentSettings, type CapacityPaymentSettingsRequest,
} from '../lib/capacity-api.ts'
import { CapacityGuardError } from '../lib/capacity-errors.ts'
import { ApiError } from '../lib/http.ts'
import type { PlainMessageKey } from '../lib/translations.ts'
import { notificationKeys } from './notification-options.ts'

export const capacityKeys = {
  all: ['admin-capacity'] as const,
  overview: (tenantId: string | null) => ['admin-capacity', 'overview', tenantId] as const,
  orders: (tenantId: string | null, limit: number, offset: number) => ['admin-capacity', 'orders', tenantId, limit, offset] as const,
  order: (id: CapacityOrderId | null) => ['admin-capacity', 'order', id] as const,
  reviewList: (status: CapacityOrderStatus, offset: number) => ['admin-capacity', 'review', status, offset] as const,
  reviewDetail: (id: CapacityOrderId | null) => ['admin-capacity', 'review-detail', id] as const,
  settings: ['admin-capacity', 'settings'] as const,
}

export function capacityOverviewOptions(tenantId: string | null, enabled: boolean) {
  return queryOptions({
    queryKey: capacityKeys.overview(tenantId),
    queryFn: ({ signal }) => fetchCapacityOverview(tenantId, signal),
    enabled, retry: false, refetchInterval: 300_000,
  })
}

export function capacityOrdersOptions(tenantId: string | null, limit: number, offset: number, enabled: boolean) {
  return queryOptions({
    queryKey: capacityKeys.orders(tenantId, limit, offset),
    queryFn: ({ signal }) => fetchCapacityOrders({ tenantId, limit, offset }, signal),
    enabled, retry: false, refetchInterval: 60_000,
  })
}

export function capacityOrderOptions(id: CapacityOrderId | null, enabled: boolean) {
  return queryOptions({
    queryKey: capacityKeys.order(id),
    queryFn: id ? ({ signal }) => fetchCapacityOrder(id, signal) : skipToken,
    enabled: enabled && id !== null, retry: false, refetchInterval: 30_000,
  })
}

export function capacityReviewListOptions(status: CapacityOrderStatus, offset: number, enabled: boolean) {
  return queryOptions({
    queryKey: capacityKeys.reviewList(status, offset),
    queryFn: ({ signal }) => fetchCapacityReviewOrders(status, offset, signal),
    enabled, retry: false, refetchInterval: 30_000,
  })
}

export function capacityReviewDetailOptions(id: CapacityOrderId | null, enabled: boolean) {
  return queryOptions({
    queryKey: capacityKeys.reviewDetail(id),
    queryFn: id ? ({ signal }) => fetchCapacityReviewOrder(id, signal) : skipToken,
    enabled: enabled && id !== null, retry: false, refetchInterval: 30_000,
  })
}

export function capacitySettingsOptions(enabled: boolean) {
  return queryOptions({
    queryKey: capacityKeys.settings,
    queryFn: ({ signal }) => fetchCapacityPaymentSettings(signal),
    enabled, retry: false,
  })
}

function refresh(client: QueryClient) {
  void client.cancelQueries({ queryKey: capacityKeys.all })
  void client.invalidateQueries({ queryKey: capacityKeys.all })
  void client.invalidateQueries({ queryKey: notificationKeys.all })
}

function refreshOnConflict(client: QueryClient, error: Error) {
  if (error instanceof ApiError && (error.status === 409 || error.status === 404 || error.status === 403)) {
    void client.cancelQueries({ queryKey: capacityKeys.all })
    void client.invalidateQueries({ queryKey: capacityKeys.all })
  }
}

function guard(allowed: boolean, key: PlainMessageKey) {
  if (!allowed) throw new CapacityGuardError(key)
}

export function capacityOrderCreateOptions(client: QueryClient, allowed: boolean) {
  return {
    mutationFn: (order: CapacityOrderRequest) => {
      guard(allowed, 'Buying extra accounts and agents is not available for this account.')
      return createCapacityOrder(order)
    },
    retry: false,
    scope: { id: 'capacity-order-create' },
    onSuccess: (order: CapacityOrder) => {
      client.setQueryData(capacityKeys.order(order.id), order)
      refresh(client)
    },
    onError: (error: Error) => { refreshOnConflict(client, error) },
  }
}

export function capacityEvidenceOptions(client: QueryClient, id: CapacityOrderId, allowed: boolean) {
  return {
    mutationFn: (file: File) => {
      guard(allowed, 'Sending payment evidence is not available for this account.')
      return submitCapacityEvidence(id, file, file.name)
    },
    retry: false,
    scope: { id: `capacity-order-${id}` },
    onSuccess: (order: CapacityOrder) => {
      client.setQueryData(capacityKeys.order(id), order)
      refresh(client)
    },
    onError: (error: Error) => { refreshOnConflict(client, error) },
  }
}

export function capacityCancelOptions(client: QueryClient, id: CapacityOrderId, allowed: boolean) {
  return {
    mutationFn: () => {
      guard(allowed, 'Cancelling an order is not available for this account.')
      return cancelCapacityOrder(id)
    },
    retry: false,
    scope: { id: `capacity-order-${id}` },
    onSuccess: (order: CapacityOrder) => {
      client.setQueryData(capacityKeys.order(id), order)
      refresh(client)
    },
    onError: (error: Error) => { refreshOnConflict(client, error) },
  }
}

export function capacityDecisionOptions(client: QueryClient, id: CapacityOrderId, allowed: boolean) {
  return {
    mutationFn: (decision: CapacityDecision) => {
      guard(allowed, 'Capacity payment review is not available for this account.')
      return decideCapacityOrder(id, decision)
    },
    retry: false,
    scope: { id: `capacity-order-${id}` },
    onSuccess: (order: CapacityOrder) => {
      client.setQueryData(capacityKeys.reviewDetail(id), order)
      refresh(client)
    },
    onError: (error: Error) => { refreshOnConflict(client, error) },
  }
}

export function capacitySettingsSaveOptions(client: QueryClient, allowed: boolean) {
  return {
    mutationFn: (settings: CapacityPaymentSettingsRequest) => {
      guard(allowed, 'Capacity payment settings are available to the genesis platform admin only.')
      return saveCapacityPaymentSettings(settings)
    },
    retry: false,
    scope: { id: 'capacity-payment-settings' },
    onSuccess: (settings: CapacityPaymentSettings) => {
      client.setQueryData(capacityKeys.settings, settings)
      refresh(client)
    },
  }
}
