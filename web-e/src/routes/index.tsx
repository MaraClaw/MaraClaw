import { useTranslation } from '@/hooks/use-translation'
import { Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { RouteErrorBoundary } from '@/components/route-error-boundary'
import { ProtectedRoute } from '@/routes/protected'
import { openclawUpdateRoute } from '@/lib/openclaw-update-policy'
import { retryingLazy } from '@/lib/retrying-import'

const AppShell = retryingLazy(() => import('@/components/layout/app-shell').then((module) => module.AppShell))
const OnboardingGate = retryingLazy(() => import('@/components/layout/onboarding-gate').then((module) => module.OnboardingGate))
const AgentLayout = retryingLazy(() => import('@/components/layout/agent-layout').then((module) => module.AgentLayout))
const AgentChannelsPage = retryingLazy(() => import('@/pages/app/agent-channels').then((module) => module.AgentChannelsPage))
const AgentPermissionsPage = retryingLazy(() => import('@/pages/app/agent-permissions').then((module) => module.AgentPermissionsPage))
const AgentRelationshipsPage = retryingLazy(() => import('@/pages/app/agent-relationships').then((module) => module.AgentRelationshipsPage))
const AgentChatPage = retryingLazy(() => import('@/pages/app/agent-chat').then((module) => module.AgentChatPage))
const AgentFilesPage = retryingLazy(() => import('@/pages/app/agent-files').then((module) => module.AgentFilesPage))
const AgentNewPage = retryingLazy(() => import('@/pages/app/agent-new').then((module) => module.AgentNewPage))
const AgentSchedulesPage = retryingLazy(() => import('@/pages/app/agent-schedules').then((module) => module.AgentSchedulesPage))
const AgentActivityPage = retryingLazy(() => import('@/pages/app/agent-activity').then((module) => module.AgentActivityPage))
const AgentSettingsPage = retryingLazy(() => import('@/pages/app/agent-settings').then((module) => module.AgentSettingsPage))
const AgentSkillsPage = retryingLazy(() => import('@/pages/app/agent-skills').then((module) => module.AgentSkillsPage))
const AgentTasksPage = retryingLazy(() => import('@/pages/app/agent-tasks').then((module) => module.AgentTasksPage))
const AgentToolsPage = retryingLazy(() => import('@/pages/app/agent-tools').then((module) => module.AgentToolsPage))
const AgentMcpPage = retryingLazy(() => import('@/pages/app/agent-mcp').then((module) => module.AgentMcpPage))
const AgentsListPage = retryingLazy(() => import('@/pages/app/agents-list').then((module) => module.AgentsListPage))
const AgentControlPage = retryingLazy(() => import('@/pages/app/agent-control').then((module) => module.AgentControlPage))
const AgentCredentialsPage = retryingLazy(() => import('@/pages/app/agent-credentials').then((module) => module.AgentCredentialsPage))
const AgentPagesPage = retryingLazy(() => import('@/pages/app/agent-pages').then((module) => module.AgentPagesPage))
const AgentPlaywrightPage = retryingLazy(() => import('@/pages/app/agent-playwright').then((module) => module.AgentPlaywrightPage))
const DirectoryPage = retryingLazy(() => import('@/pages/app/directory').then((module) => module.DirectoryPage))
const AdmissionDetailPage = retryingLazy(() => import('@/pages/app/admission-detail').then((module) => module.AdmissionDetailPage))
const TransferDetailPage = retryingLazy(() => import('@/pages/app/transfer-detail').then((module) => module.TransferDetailPage))
const NotificationsPage = retryingLazy(() => import('@/pages/app/notifications').then((module) => module.NotificationsPage))
const OrganizationPaymentPage = retryingLazy(() => import('@/pages/organization-payment').then((module) => module.OrganizationPaymentPage))
const OpenClawUpdateDetailPage = retryingLazy(() => import('@/pages/app/openclaw-update-detail').then((module) => module.OpenClawUpdateDetailPage))
const OkrPage = retryingLazy(() => import('@/pages/app/okr').then((module) => module.OkrPage))
const OnboardingPage = retryingLazy(() => import('@/pages/app/onboarding').then((module) => module.OnboardingPage))
const ClawbookPage = retryingLazy(() => import('@/pages/app/clawbook').then((module) => module.ClawbookPage))
const SettingsPage = retryingLazy(() => import('@/pages/app/settings').then((module) => module.SettingsPage))
const ForgotPasswordPage = retryingLazy(() => import('@/pages/forgot-password').then((module) => module.ForgotPasswordPage))
const JoinOrgPage = retryingLazy(() => import('@/pages/join-org').then((module) => module.JoinOrgPage))
const LandingPage = retryingLazy(() => import('@/pages/landing').then((module) => module.LandingPage))
const LoginPage = retryingLazy(() => import('@/pages/login').then((module) => module.LoginPage))
const RegisterPage = retryingLazy(() => import('@/pages/register').then((module) => module.RegisterPage))
const ResetPasswordPage = retryingLazy(() => import('@/pages/reset-password').then((module) => module.ResetPasswordPage))
const SsoCallbackPage = retryingLazy(() => import('@/pages/sso-callback').then((module) => module.SsoCallbackPage))
const TransferPage = retryingLazy(() => import('@/pages/transfer').then((module) => module.TransferPage))
const VerifyEmailPage = retryingLazy(() => import('@/pages/verify-email').then((module) => module.VerifyEmailPage))

export function AppRouter() {
  const { t: translate } = useTranslation()
  return (
    <BrowserRouter>
      <Suspense fallback={<div role="status" className="flex min-h-svh items-center justify-center bg-background text-sm text-muted-foreground">{translate("Loading…")}</div>}>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/verify-email" element={<VerifyEmailPage />} />
          <Route path="/sso/callback" element={<SsoCallbackPage />} />
          <Route path="/organization-payment" element={<OrganizationPaymentPage />} />
          <Route path="/join" element={<JoinOrgPage />} />
          <Route path="/transfer" element={<TransferPage />} />

          <Route path="/app" element={<ProtectedRoute />}>
            <Route element={<AppShell />}>
              <Route element={<RouteErrorBoundary />}>
                <Route path="account" element={<Navigate to="/app/settings" replace />} />
                <Route path="settings" element={<SettingsPage />} />
                <Route path="admissions" element={<Navigate to="/app/notifications?category=admissions" replace />} />
                <Route path="admissions/:id" element={<AdmissionDetailPage />} />
                <Route path="admissions/transfers/:id" element={<TransferDetailPage />} />
                <Route path="notifications" element={<NotificationsPage />} />
                <Route element={<OnboardingGate />}>
                  <Route index element={<Navigate to="agents" replace />} />
                  <Route path="onboarding" element={<OnboardingPage />} />
                  <Route path={openclawUpdateRoute} element={<OpenClawUpdateDetailPage />} />
                  <Route path="clawbook" element={<ClawbookPage />} />
                  <Route path="okr" element={<OkrPage />} />
                  <Route path="directory" element={<DirectoryPage />} />
                  <Route path="agents" element={<AgentsListPage />} />
                  <Route path="agents/new" element={<AgentNewPage />} />
                  <Route path="agents/:agentId" element={<AgentLayout />}>
                    <Route element={<RouteErrorBoundary />}>
                      <Route index element={<Navigate to="chat" replace />} />
                      <Route path="chat" element={<AgentChatPage />} />
                      <Route path="chat/:sessionId" element={<AgentChatPage />} />
                      <Route path="files" element={<AgentFilesPage />} />
                      <Route path="skills" element={<AgentSkillsPage />} />
                      <Route path="tools" element={<AgentToolsPage />} />
                      <Route path="mcp" element={<AgentMcpPage />} />
                      <Route path="tasks" element={<AgentTasksPage />} />
                      <Route path="schedules" element={<AgentSchedulesPage />} />
                      <Route path="activity" element={<AgentActivityPage />} />
                      <Route path="channels" element={<AgentChannelsPage />} />
                      <Route path="relationships" element={<AgentRelationshipsPage />} />
                      <Route path="permissions" element={<AgentPermissionsPage />} />
                      <Route path="control" element={<AgentControlPage />} />
                      <Route path="credentials" element={<AgentCredentialsPage />} />
                      <Route path="pages" element={<AgentPagesPage />} />
                      <Route path="playwright" element={<AgentPlaywrightPage />} />
                      <Route path="settings" element={<AgentSettingsPage />} />
                    </Route>
                  </Route>
                </Route>
              </Route>
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
