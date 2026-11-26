import { Suspense } from 'react'
import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'

import { AdminShell } from '@/components/layout/admin-shell'
import { RouteErrorBoundary } from '@/components/route-error-boundary'
import { useAuth } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { retryingLazy } from '@/lib/retrying-import'
import { canManageInvitations, canReviewAdmissions, canReviewAgentDeletions, canReviewCapacityPayments, canReviewOrganizationAdmissions, canSendBroadcast, canViewCapacityOrders, canViewClawbook } from '@/lib/types/auth'
import { PlatformAdminRoute } from '@/routes/platform-admin'
import { ProtectedRoute } from '@/routes/protected'
import { TransferReviewRoute } from '@/routes/transfer-review'

const BroadcastPage = retryingLazy(() => import('@/pages/broadcast').then((module) => module.BroadcastPage))
const InvitationsPage = retryingLazy(() => import('@/pages/invitations').then((module) => module.InvitationsPage))
const ForgotPasswordPage = retryingLazy(() => import('@/pages/forgot-password').then((module) => module.ForgotPasswordPage))
const LoginPage = retryingLazy(() => import('@/pages/login').then((module) => module.LoginPage))
const CompaniesPage = retryingLazy(() => import('@/pages/companies').then((module) => module.CompaniesPage))
const CompanyDetailPage = retryingLazy(() => import('@/pages/company-detail').then((module) => module.CompanyDetailPage))
const CompanyRuntimePage = retryingLazy(() => import('@/pages/company-runtime-page').then((module) => module.CompanyRuntimePage))
const OverviewPage = retryingLazy(() => import('@/pages/overview').then((module) => module.OverviewPage))
const OpenClawUpdatesPage = retryingLazy(() => import('@/pages/openclaw-updates').then((module) => module.OpenClawUpdatesPage))
const UpgradeSignalsPage = retryingLazy(() => import('@/pages/upgrade-signals').then((module) => module.UpgradeSignalsPage))
const UpgradeSignalsDetailPage = retryingLazy(() => import('@/pages/upgrade-signals-detail').then((module) => module.UpgradeSignalsDetailPage))
const ToolsPage = retryingLazy(() => import('@/pages/tools').then((module) => module.ToolsPage))
const ResetPasswordPage = retryingLazy(() => import('@/pages/reset-password').then((module) => module.ResetPasswordPage))
const SearchEnginePage = retryingLazy(() => import('@/pages/search-engine').then((module) => module.SearchEnginePage))
const SettingsPage = retryingLazy(() => import('@/pages/settings').then((module) => module.SettingsPage))
const LlmModelsPage = retryingLazy(() => import('@/pages/llm-models').then((module) => module.LlmModelsPage))
const UserDetailPage = retryingLazy(() => import('@/pages/user-detail').then((module) => module.UserDetailPage))
const UsersPage = retryingLazy(() => import('@/pages/users').then((module) => module.UsersPage))
const NotificationsPage = retryingLazy(() => import('@/pages/notifications').then((module) => module.NotificationsPage))
const AdminAuditPage = retryingLazy(() => import('@/pages/admin-audit').then((module) => module.AdminAuditPage))
const AdmissionApprovalsPage = retryingLazy(() => import('@/pages/admission-approvals').then((module) => module.AdmissionApprovalsPage))
const AdmissionApprovalDetailPage = retryingLazy(() => import('@/pages/admission-approval-detail').then((module) => module.AdmissionApprovalDetailPage))
const PaymentsPage = retryingLazy(() => import('@/pages/payments').then((module) => module.PaymentsPage))
const TransferPaymentDetailPage = retryingLazy(() => import('@/pages/transfer-payment-detail').then((module) => module.TransferPaymentDetailPage))
const AgentDeletionRequestsPage = retryingLazy(() => import('@/pages/agent-deletion-requests').then((module) => module.AgentDeletionRequestsPage))
const AgentDeletionRequestDetailPage = retryingLazy(() => import('@/pages/agent-deletion-request-detail').then((module) => module.AgentDeletionRequestDetailPage))
const ClawbookPage = retryingLazy(() => import('@/pages/clawbook').then((module) => module.ClawbookPage))
const CapacityOrderPage = retryingLazy(() => import('@/pages/capacity-order').then((module) => module.CapacityOrderPage))
const CapacityPaymentDetailPage = retryingLazy(() => import('@/pages/capacity-payment-detail').then((module) => module.CapacityPaymentDetailPage))

function BroadcastRoute() {
  const { user } = useAuth()
  return canSendBroadcast(user) ? <Outlet /> : <Navigate to="/" replace />
}

function InvitationsRoute() {
  const { user } = useAuth()
  return canManageInvitations(user) ? <Outlet /> : <Navigate to="/" replace />
}

function AdmissionReviewRoute() {
  const { user } = useAuth()
  return canReviewAdmissions(user) ? <Outlet /> : <Navigate to="/" replace />
}

function OrganizationAdmissionReviewRoute() {
  const { user } = useAuth()
  return canReviewOrganizationAdmissions(user) ? <Outlet /> : <Navigate to="/" replace />
}

function AgentDeletionReviewRoute() {
  const { user } = useAuth()
  return canReviewAgentDeletions(user) ? <Outlet /> : <Navigate to="/" replace />
}

function ClawbookRoute() {
  const { user } = useAuth()
  return canViewClawbook(user) ? <Outlet /> : <Navigate to="/" replace />
}

function CapacityReviewRoute() {
  const { user } = useAuth()
  return canReviewCapacityPayments(user) ? <Outlet /> : <Navigate to="/" replace />
}

function CapacityOrderRoute() {
  const { user } = useAuth()
  return canViewCapacityOrders(user) ? <Outlet /> : <Navigate to="/" replace />
}

export function AppRouter() {
  const { t } = useTranslation()
  return (
    <BrowserRouter>
      <Suspense fallback={<div role="status" className="flex min-h-svh items-center justify-center bg-background text-sm text-muted-foreground">{t('Loading…')}</div>}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />

          <Route element={<ProtectedRoute />}>
            <Route element={<AdminShell />}>
              <Route element={<RouteErrorBoundary />}>
                <Route index element={<OverviewPage />} />
                <Route path="account" element={<Navigate to="/settings" replace />} />
                <Route path="notifications" element={<NotificationsPage />} />
                <Route path="activity" element={<AdminAuditPage />} />
                <Route element={<AgentDeletionReviewRoute />}>
                  <Route path="agent-deletion-requests" element={<AgentDeletionRequestsPage />} />
                  <Route path="agent-deletion-requests/:id" element={<AgentDeletionRequestDetailPage />} />
                </Route>
                <Route element={<TransferReviewRoute />}>
                  <Route path="payments" element={<PaymentsPage />} />
                  <Route path="transfer-payments" element={<Navigate to="/payments" replace />} />
                  <Route path="transfer-payments/:id" element={<TransferPaymentDetailPage />} />
                </Route>
                <Route element={<CapacityReviewRoute />}>
                  <Route path="capacity-payments" element={<Navigate to="/payments?tab=capacity" replace />} />
                  <Route path="capacity-payments/:orderId" element={<CapacityPaymentDetailPage />} />
                </Route>
                <Route element={<CapacityOrderRoute />}>
                  <Route path="capacity-orders/:orderId" element={<CapacityOrderPage />} />
                </Route>
                <Route element={<AdmissionReviewRoute />}>
                  <Route path="admission-approvals" element={<AdmissionApprovalsPage />} />
                  <Route path="admission-approvals/:id" element={<AdmissionApprovalDetailPage />} />
                </Route>
                <Route element={<OrganizationAdmissionReviewRoute />}>
                  <Route path="organization-admissions" element={<Navigate to="/payments?tab=admissions" replace />} />
                </Route>
                <Route element={<BroadcastRoute />}>
                  <Route path="broadcast" element={<BroadcastPage />} />
                </Route>
                <Route element={<InvitationsRoute />}>
                  <Route path="invitations" element={<InvitationsPage />} />
                </Route>
                <Route element={<PlatformAdminRoute />}>
                  <Route path="openclaw-updates" element={<OpenClawUpdatesPage />} />
                  <Route path="upgrade-signals" element={<UpgradeSignalsPage />} />
                  <Route path="upgrade-signals/:tenantId" element={<UpgradeSignalsDetailPage />} />
                  <Route path="companies" element={<CompaniesPage />} />
                  <Route path="companies/:companyId" element={<CompanyDetailPage />} />
                  <Route path="companies/:companyId/runtimes" element={<CompanyRuntimePage />} />
                  <Route path="search-engine" element={<SearchEnginePage />} />
                </Route>
                <Route path="users" element={<UsersPage />} />
                <Route path="users/:userId" element={<UserDetailPage />} />
                <Route element={<ClawbookRoute />}>
                  <Route path="clawbook" element={<ClawbookPage />} />
                </Route>
                <Route path="models" element={<LlmModelsPage />} />
                <Route path="tools" element={<ToolsPage />} />
                <Route path="settings" element={<SettingsPage />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
