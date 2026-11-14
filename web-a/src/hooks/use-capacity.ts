import { useQuery, useQueryClient } from '@tanstack/react-query'

import type { CapacityOrderId, CapacityOrderStatus } from '@/lib/capacity-api'
import {
  canBuyCapacity, canManageCapacityPaymentSettings, canReviewCapacityPayments, canViewCapacityOrders, isPlatformAdminUser,
} from '@/lib/types/auth'
import {
  capacityCancelOptions, capacityDecisionOptions, capacityEvidenceOptions, capacityOrderCreateOptions, capacityOrderOptions,
  capacityOrdersOptions, capacityOverviewOptions, capacityReviewDetailOptions, capacityReviewListOptions,
  capacitySettingsOptions, capacitySettingsSaveOptions,
} from './capacity-options'
import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'

export function useCapacityAccess() {
  const { user, mustChangePassword } = useAuth()
  return {
    canBuy: !mustChangePassword && canBuyCapacity(user),
    canView: !mustChangePassword && canViewCapacityOrders(user),
    canReview: !mustChangePassword && canReviewCapacityPayments(user),
    canManageSettings: !mustChangePassword && canManageCapacityPaymentSettings(user),
  }
}

/** Organization admins read their own company (no `tenant_id`); platform admins name the company they are looking at. */
function useCompanyScope(tenantId: string | null) {
  const { user } = useAuth()
  return isPlatformAdminUser(user) ? tenantId : null
}

export function useCapacityOverview(tenantId: string | null) {
  const { canView } = useCapacityAccess()
  return useQuery(capacityOverviewOptions(useCompanyScope(tenantId), canView && tenantId !== null))
}

export function useCapacityOrders(tenantId: string | null, limit: number, offset = 0) {
  const { canView } = useCapacityAccess()
  return useQuery(capacityOrdersOptions(useCompanyScope(tenantId), limit, offset, canView && tenantId !== null))
}

export function useCapacityOrder(id: CapacityOrderId | null) {
  return useQuery(capacityOrderOptions(id, useCapacityAccess().canView))
}

export function useCapacityReviewOrders(status: CapacityOrderStatus, offset: number) {
  return useQuery(capacityReviewListOptions(status, offset, useCapacityAccess().canReview))
}

export function useCapacityReviewOrder(id: CapacityOrderId | null) {
  return useQuery(capacityReviewDetailOptions(id, useCapacityAccess().canReview))
}

export function useCapacityPaymentSettings() {
  return useQuery(capacitySettingsOptions(useCapacityAccess().canManageSettings))
}

export function useCapacityOrderCreate() {
  return useSessionMutation(capacityOrderCreateOptions(useQueryClient(), useCapacityAccess().canBuy))
}

export function useCapacityEvidenceSubmit(id: CapacityOrderId) {
  return useSessionMutation(capacityEvidenceOptions(useQueryClient(), id, useCapacityAccess().canBuy))
}

export function useCapacityCancel(id: CapacityOrderId) {
  return useSessionMutation(capacityCancelOptions(useQueryClient(), id, useCapacityAccess().canBuy))
}

export function useCapacityDecision(id: CapacityOrderId) {
  return useSessionMutation(capacityDecisionOptions(useQueryClient(), id, useCapacityAccess().canReview))
}

export function useCapacitySettingsSave() {
  return useSessionMutation(capacitySettingsSaveOptions(useQueryClient(), useCapacityAccess().canManageSettings))
}
