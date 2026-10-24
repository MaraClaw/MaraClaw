import { Navigate, Outlet } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useTransferReviewAccess } from '@/hooks/use-transfer-reviews'
import { ApiError } from '@/lib/http'

export function TransferReviewRoute() {
  const access = useTransferReviewAccess()
  if (access.isPending) return <p role="status" className="text-sm text-muted-foreground">Checking payment review access...</p>
  if (access.error) return <Card className="mx-auto max-w-3xl"><CardContent className="flex flex-col items-start gap-3">
    <h1 className="font-display text-2xl font-semibold">Payment review access</h1>
    <p role="alert" className="text-sm">{access.error instanceof ApiError ? access.error.message : 'Unable to check your organization. Payment review remains locked.'}</p>
    <Button variant="outline" disabled={access.isFetching} onClick={() => { void access.retry() }}>Retry access check</Button>
  </CardContent></Card>
  return access.canReview ? <Outlet /> : <Navigate to="/" replace />
}
