import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/app-shell'
import { OnboardingGate } from '@/components/layout/onboarding-gate'
import { AgentLayout } from '@/components/layout/agent-layout'
import { AccountPage } from '@/pages/app/account'
import { AgentChannelsPage } from '@/pages/app/agent-channels'
import { AgentPermissionsPage } from '@/pages/app/agent-permissions'
import { AgentRelationshipsPage } from '@/pages/app/agent-relationships'
import { AgentChatPage } from '@/pages/app/agent-chat'
import { AgentFilesPage } from '@/pages/app/agent-files'
import { AgentNewPage } from '@/pages/app/agent-new'
import { AgentSchedulesPage } from '@/pages/app/agent-schedules'
import { AgentSettingsPage } from '@/pages/app/agent-settings'
import { AgentSkillsPage } from '@/pages/app/agent-skills'
import { AgentTasksPage } from '@/pages/app/agent-tasks'
import { AgentToolsPage } from '@/pages/app/agent-tools'
import { AgentsListPage } from '@/pages/app/agents-list'
import { NotificationsPage } from '@/pages/app/notifications'
import { OnboardingPage } from '@/pages/app/onboarding'
import { SettingsPage } from '@/pages/app/settings'
import { ForgotPasswordPage } from '@/pages/forgot-password'
import { JoinOrgPage } from '@/pages/join-org'
import { LandingPage } from '@/pages/landing'
import { LoginPage } from '@/pages/login'
import { RegisterPage } from '@/pages/register'
import { ResetPasswordPage } from '@/pages/reset-password'
import { SsoCallbackPage } from '@/pages/sso-callback'
import { TransferPage } from '@/pages/transfer'
import { VerifyEmailPage } from '@/pages/verify-email'
import { ProtectedRoute } from '@/routes/protected'

export function AppRouter() {
  return (
    <BrowserRouter>
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
            <Route element={<OnboardingGate />}>
              <Route index element={<Navigate to="agents" replace />} />
              <Route path="onboarding" element={<OnboardingPage />} />
              <Route path="account" element={<AccountPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="notifications" element={<NotificationsPage />} />
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
                <Route path="settings" element={<AgentSettingsPage />} />
              </Route>
            </Route>
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
