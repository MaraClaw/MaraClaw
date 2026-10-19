import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/app-shell'
import { OnboardingGate } from '@/components/layout/onboarding-gate'
import { AgentLayout } from '@/components/layout/agent-layout'
import { ProtectedRoute } from '@/routes/protected'

const AgentChannelsPage = lazy(() => import('@/pages/app/agent-channels').then((module) => ({ default: module.AgentChannelsPage })))
const AgentPermissionsPage = lazy(() => import('@/pages/app/agent-permissions').then((module) => ({ default: module.AgentPermissionsPage })))
const AgentRelationshipsPage = lazy(() => import('@/pages/app/agent-relationships').then((module) => ({ default: module.AgentRelationshipsPage })))
const AgentChatPage = lazy(() => import('@/pages/app/agent-chat').then((module) => ({ default: module.AgentChatPage })))
const AgentFilesPage = lazy(() => import('@/pages/app/agent-files').then((module) => ({ default: module.AgentFilesPage })))
const AgentNewPage = lazy(() => import('@/pages/app/agent-new').then((module) => ({ default: module.AgentNewPage })))
const AgentSchedulesPage = lazy(() => import('@/pages/app/agent-schedules').then((module) => ({ default: module.AgentSchedulesPage })))
const AgentSettingsPage = lazy(() => import('@/pages/app/agent-settings').then((module) => ({ default: module.AgentSettingsPage })))
const AgentSkillsPage = lazy(() => import('@/pages/app/agent-skills').then((module) => ({ default: module.AgentSkillsPage })))
const AgentTasksPage = lazy(() => import('@/pages/app/agent-tasks').then((module) => ({ default: module.AgentTasksPage })))
const AgentToolsPage = lazy(() => import('@/pages/app/agent-tools').then((module) => ({ default: module.AgentToolsPage })))
const AgentsListPage = lazy(() => import('@/pages/app/agents-list').then((module) => ({ default: module.AgentsListPage })))
const AgentControlPage = lazy(() => import('@/pages/app/agent-control').then((module) => ({ default: module.AgentControlPage })))
const AgentCredentialsPage = lazy(() => import('@/pages/app/agent-credentials').then((module) => ({ default: module.AgentCredentialsPage })))
const AgentPagesPage = lazy(() => import('@/pages/app/agent-pages').then((module) => ({ default: module.AgentPagesPage })))
const AgentPlaywrightPage = lazy(() => import('@/pages/app/agent-playwright').then((module) => ({ default: module.AgentPlaywrightPage })))
const DirectoryPage = lazy(() => import('@/pages/app/directory').then((module) => ({ default: module.DirectoryPage })))
const NotificationsPage = lazy(() => import('@/pages/app/notifications').then((module) => ({ default: module.NotificationsPage })))
const OkrPage = lazy(() => import('@/pages/app/okr').then((module) => ({ default: module.OkrPage })))
const OnboardingPage = lazy(() => import('@/pages/app/onboarding').then((module) => ({ default: module.OnboardingPage })))
const PlazaPage = lazy(() => import('@/pages/app/plaza').then((module) => ({ default: module.PlazaPage })))
const SettingsPage = lazy(() => import('@/pages/app/settings').then((module) => ({ default: module.SettingsPage })))
const ForgotPasswordPage = lazy(() => import('@/pages/forgot-password').then((module) => ({ default: module.ForgotPasswordPage })))
const JoinOrgPage = lazy(() => import('@/pages/join-org').then((module) => ({ default: module.JoinOrgPage })))
const LandingPage = lazy(() => import('@/pages/landing').then((module) => ({ default: module.LandingPage })))
const LoginPage = lazy(() => import('@/pages/login').then((module) => ({ default: module.LoginPage })))
const RegisterPage = lazy(() => import('@/pages/register').then((module) => ({ default: module.RegisterPage })))
const ResetPasswordPage = lazy(() => import('@/pages/reset-password').then((module) => ({ default: module.ResetPasswordPage })))
const SsoCallbackPage = lazy(() => import('@/pages/sso-callback').then((module) => ({ default: module.SsoCallbackPage })))
const TransferPage = lazy(() => import('@/pages/transfer').then((module) => ({ default: module.TransferPage })))
const VerifyEmailPage = lazy(() => import('@/pages/verify-email').then((module) => ({ default: module.VerifyEmailPage })))

export function AppRouter() {
  return (
    <BrowserRouter>
      <Suspense fallback={<div role="status" className="flex min-h-svh items-center justify-center bg-background text-sm text-muted-foreground">Loading…</div>}>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/verify-email" element={<VerifyEmailPage />} />
          <Route path="/sso/callback" element={<SsoCallbackPage />} />
          <Route path="/join" element={<JoinOrgPage />} />
          <Route path="/transfer" element={<TransferPage />} />

          <Route path="/app" element={<ProtectedRoute />}>
            <Route element={<AppShell />}>
              <Route path="account" element={<Navigate to="/app/settings" replace />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route element={<OnboardingGate />}>
                <Route index element={<Navigate to="agents" replace />} />
                <Route path="onboarding" element={<OnboardingPage />} />
                <Route path="notifications" element={<NotificationsPage />} />
                <Route path="plaza" element={<PlazaPage />} />
                <Route path="okr" element={<OkrPage />} />
                <Route path="directory" element={<DirectoryPage />} />
                <Route path="agents" element={<AgentsListPage />} />
                <Route path="agents/new" element={<AgentNewPage />} />
                <Route path="agents/:agentId" element={<AgentLayout />}>
                  <Route index element={<Navigate to="chat" replace />} />
                  <Route path="chat" element={<AgentChatPage />} />
                  <Route path="chat/:sessionId" element={<AgentChatPage />} />
                  <Route path="files" element={<AgentFilesPage />} />
                  <Route path="skills" element={<AgentSkillsPage />} />
                  <Route path="tools" element={<AgentToolsPage />} />
                  <Route path="tasks" element={<AgentTasksPage />} />
                  <Route path="schedules" element={<AgentSchedulesPage />} />
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
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
