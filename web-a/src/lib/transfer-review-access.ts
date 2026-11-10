import type { TransferSummary } from './transfer-reviews-api.ts'
import { canReviewAdmissions, isPlatformAdminUser, type UserOut } from './types/auth.ts'

export function canReviewTransferPayments(user: UserOut | null | undefined): boolean {
  return user?.is_active === true && isPlatformAdminUser(user)
}

export function canOpenTransferAdmission(user: UserOut | null | undefined, request: TransferSummary): boolean {
  return user?.is_active === true && !isPlatformAdminUser(user) && canReviewAdmissions(user)
    && user.tenant_id === request.target_tenant_id && request.admission_request_id !== null
}

export function isPendingTransferPayment(request: TransferSummary): boolean {
  return request.choice === 'paid' && request.status === 'payment_review'
}
