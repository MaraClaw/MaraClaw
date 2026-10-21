import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'

import { AdminShell } from '@/components/layout/admin-shell'
import { useAuth } from '@/hooks/use-auth'
import { canReviewAdmissions, canSendBroadcast } from '@/lib/types/auth'
import { PlatformAdminRoute } from '@/routes/platform-admin'
import { ProtectedRoute } from '@/routes/protected'

const AccountPage = lazy(() => import('@/pages/account').then((module) => ({ default: module.AccountPage })))
const BroadcastPage = lazy(() => import('@/pages/broadcast').then((module) => ({ default: module.BroadcastPage })))
const ForgotPasswordPage = lazy(() => import('@/pages/forgot-password').then((module) => ({ default: module.ForgotPasswordPage })))
const LoginPage = lazy(() => import('@/pages/login').then((module) => ({ default: module.LoginPage })))
const CompaniesPage = lazy(() => import('@/pages/companies').then((module) => ({ default: module.CompaniesPage })))
const CompanyDetailPage = lazy(() => import('@/pages/company-detail').then((module) => ({ default: module.CompanyDetailPage })))
const OverviewPage = lazy(() => import('@/pages/overview').then((module) => ({ default: module.OverviewPage })))
const PlaceholderPage = lazy(() => import('@/pages/placeholder').then((module) => ({ default: module.PlaceholderPage })))
const ResetPasswordPage = lazy(() => import('@/pages/reset-password').then((module) => ({ default: module.ResetPasswordPage })))
const SearchEnginePage = lazy(() => import('@/pages/search-engine').then((module) => ({ default: module.SearchEnginePage })))
const SettingsPage = lazy(() => import('@/pages/settings').then((module) => ({ default: module.SettingsPage })))
const LlmModelsPage = lazy(() => import('@/pages/llm-models').then((module) => ({ default: module.LlmModelsPage })))
const UserDetailPage = lazy(() => import('@/pages/user-detail').then((module) => ({ default: module.UserDetailPage })))
const UsersPage = lazy(() => import('@/pages/users').then((module) => ({ default: module.UsersPage })))
const NotificationsPage = lazy(() => import('@/pages/notifications').then((module) => ({ default: module.NotificationsPage })))
const AdmissionApprovalsPage = lazy(() => import('@/pages/admission-approvals').then((module) => ({ default: module.AdmissionApprovalsPage })))
const AdmissionApprovalDetailPage = lazy(() => import('@/pages/admission-approval-detail').then((module) => ({ default: module.AdmissionApprovalDetailPage })))

function BroadcastRoute() {
  const { user } = useAuth()
  return canSendBroadcast(user) ? <Outlet /> : <Navigate to="/" replace />
}

function AdmissionReviewRoute() {
  const { user } = useAuth()
  return canReviewAdmissions(user) ? <Outlet /> : <Navigate to="/" replace />
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
              <Route element={<AdmissionReviewRoute />}>
                <Route path="admission-approvals" element={<AdmissionApprovalsPage />} />
                <Route path="admission-approvals/:id" element={<AdmissionApprovalDetailPage />} />
              </Route>
              <Route element={<BroadcastRoute />}>
                <Route path="broadcast" element={<BroadcastPage />} />
              </Route>
              <Route element={<PlatformAdminRoute />}>
                <Route path="companies" element={<CompaniesPage />} />
                <Route path="companies/:companyId" element={<CompanyDetailPage />} />
                <Route path="search-engine" element={<SearchEnginePage />} />
              </Route>
              <Route path="users" element={<UsersPage />} />
              <Route path="users/:userId" element={<UserDetailPage />} />
              <Route path="models" element={<LlmModelsPage />} />
              <Route
                path="tools"
                element={
                  <PlaceholderPage
                    title="Tools"
                    description="Platform and tenant tool catalog management."
                    apiHint="/api/tools"
                  />
                }
              />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
