import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'

import { AdminShell } from '@/components/layout/admin-shell'
import { useAuth } from '@/hooks/use-auth'
import { canManageInvitations, canReviewAdmissions, canReviewAgentDeletions, canSendBroadcast } from '@/lib/types/auth'
import { PlatformAdminRoute } from '@/routes/platform-admin'
import { ProtectedRoute } from '@/routes/protected'
import { TransferReviewRoute } from '@/routes/transfer-review'

const AccountPage = lazy(() => import('@/pages/account').then((module) => ({ default: module.AccountPage })))
const BroadcastPage = lazy(() => import('@/pages/broadcast').then((module) => ({ default: module.BroadcastPage })))
const InvitationsPage = lazy(() => import('@/pages/invitations').then((module) => ({ default: module.InvitationsPage })))
const ForgotPasswordPage = lazy(() => import('@/pages/forgot-password').then((module) => ({ default: module.ForgotPasswordPage })))
const LoginPage = lazy(() => import('@/pages/login').then((module) => ({ default: module.LoginPage })))
const CompaniesPage = lazy(() => import('@/pages/companies').then((module) => ({ default: module.CompaniesPage })))
const CompanyDetailPage = lazy(() => import('@/pages/company-detail').then((module) => ({ default: module.CompanyDetailPage })))
const OverviewPage = lazy(() => import('@/pages/overview').then((module) => ({ default: module.OverviewPage })))
const OpenClawUpdatesPage = lazy(() => import('@/pages/openclaw-updates').then((module) => ({ default: module.OpenClawUpdatesPage })))
const ToolsPage = lazy(() => import('@/pages/tools').then((module) => ({ default: module.ToolsPage })))
const ResetPasswordPage = lazy(() => import('@/pages/reset-password').then((module) => ({ default: module.ResetPasswordPage })))
const SearchEnginePage = lazy(() => import('@/pages/search-engine').then((module) => ({ default: module.SearchEnginePage })))
const SettingsPage = lazy(() => import('@/pages/settings').then((module) => ({ default: module.SettingsPage })))
const LlmModelsPage = lazy(() => import('@/pages/llm-models').then((module) => ({ default: module.LlmModelsPage })))
const UserDetailPage = lazy(() => import('@/pages/user-detail').then((module) => ({ default: module.UserDetailPage })))
const UsersPage = lazy(() => import('@/pages/users').then((module) => ({ default: module.UsersPage })))
const NotificationsPage = lazy(() => import('@/pages/notifications').then((module) => ({ default: module.NotificationsPage })))
const AdminAuditPage = lazy(() => import('@/pages/admin-audit').then((module) => ({ default: module.AdminAuditPage })))
const AdmissionApprovalsPage = lazy(() => import('@/pages/admission-approvals').then((module) => ({ default: module.AdmissionApprovalsPage })))
const AdmissionApprovalDetailPage = lazy(() => import('@/pages/admission-approval-detail').then((module) => ({ default: module.AdmissionApprovalDetailPage })))
const TransferPaymentsPage = lazy(() => import('@/pages/transfer-payments').then((module) => ({ default: module.TransferPaymentsPage })))
const TransferPaymentDetailPage = lazy(() => import('@/pages/transfer-payment-detail').then((module) => ({ default: module.TransferPaymentDetailPage })))
const AgentDeletionRequestsPage = lazy(() => import('@/pages/agent-deletion-requests').then((module) => ({ default: module.AgentDeletionRequestsPage })))
const AgentDeletionRequestDetailPage = lazy(() => import('@/pages/agent-deletion-request-detail').then((module) => ({ default: module.AgentDeletionRequestDetailPage })))

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

function AgentDeletionReviewRoute() {
  const { user } = useAuth()
  return canReviewAgentDeletions(user) ? <Outlet /> : <Navigate to="/" replace />
}

export function AppRouter() {
  return (
    <BrowserRouter>
      <Suspense fallback={<div role="status" className="flex min-h-svh items-center justify-center bg-background text-sm text-muted-foreground">Loading…</div>}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />

          <Route element={<ProtectedRoute />}>
            <Route element={<AdminShell />}>
              <Route index element={<OverviewPage />} />
              <Route path="account" element={<AccountPage />} />
              <Route path="notifications" element={<NotificationsPage />} />
              <Route path="activity" element={<AdminAuditPage />} />
              <Route element={<AgentDeletionReviewRoute />}>
                <Route path="agent-deletion-requests" element={<AgentDeletionRequestsPage />} />
                <Route path="agent-deletion-requests/:id" element={<AgentDeletionRequestDetailPage />} />
              </Route>
              <Route element={<TransferReviewRoute />}>
                <Route path="transfer-payments" element={<TransferPaymentsPage />} />
                <Route path="transfer-payments/:id" element={<TransferPaymentDetailPage />} />
              </Route>
              <Route element={<AdmissionReviewRoute />}>
                <Route path="admission-approvals" element={<AdmissionApprovalsPage />} />
                <Route path="admission-approvals/:id" element={<AdmissionApprovalDetailPage />} />
              </Route>
              <Route element={<BroadcastRoute />}>
                <Route path="broadcast" element={<BroadcastPage />} />
              </Route>
              <Route element={<InvitationsRoute />}>
                <Route path="invitations" element={<InvitationsPage />} />
              </Route>
              <Route element={<PlatformAdminRoute />}>
                <Route path="openclaw-updates" element={<OpenClawUpdatesPage />} />
                <Route path="companies" element={<CompaniesPage />} />
                <Route path="companies/:companyId" element={<CompanyDetailPage />} />
                <Route path="search-engine" element={<SearchEnginePage />} />
              </Route>
              <Route path="users" element={<UsersPage />} />
              <Route path="users/:userId" element={<UserDetailPage />} />
              <Route path="models" element={<LlmModelsPage />} />
              <Route path="tools" element={<ToolsPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
