import type { ReviewTenant, TransferSummary } from './transfer-reviews-api.ts'
import { canReviewAdmissions, isPlatformAdminUser, type UserOut } from './types/auth.ts'

export function canInspectReviewTenant(user: UserOut | null | undefined): boolean {
  return user?.is_active === true && !isPlatformAdminUser(user) && user.role === 'org_admin'
    && user.is_genesis === true && user.tenant_id !== null
}

export function canReviewTransferPayments(user: UserOut | null | undefined, tenant?: ReviewTenant): boolean {
  if (!user?.is_active) return false
  if (isPlatformAdminUser(user)) return true
  return canInspectReviewTenant(user) && tenant?.id === user.tenant_id
    && tenant?.is_active === true && tenant.is_default_end_user_org === true
}

export function canOpenTransferAdmission(user: UserOut | null | undefined, request: TransferSummary): boolean {
  return user?.is_active === true && !isPlatformAdminUser(user) && canReviewAdmissions(user)
    && user.tenant_id === request.target_tenant_id && request.admission_request_id !== null
}

export function isPendingTransferPayment(request: TransferSummary): boolean {
  return request.choice === 'paid' && request.status === 'payment_review'
}
