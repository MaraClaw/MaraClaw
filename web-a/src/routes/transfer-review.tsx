import { Navigate, Outlet } from 'react-router-dom'

import { useTransferReviewAccess } from '@/hooks/use-transfer-reviews'

export function TransferReviewRoute() {
  const access = useTransferReviewAccess()
  return access.canReview ? <Outlet /> : <Navigate to="/" replace />
}
