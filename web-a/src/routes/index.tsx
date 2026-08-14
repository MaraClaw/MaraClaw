import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AdminShell } from '@/components/layout/admin-shell'
import { AccountPage } from '@/pages/account'
import { ForgotPasswordPage } from '@/pages/forgot-password'
import { LoginPage } from '@/pages/login'
import { CompaniesPage } from '@/pages/companies'
import { CompanyDetailPage } from '@/pages/company-detail'
import { OverviewPage } from '@/pages/overview'
import { PlaceholderPage } from '@/pages/placeholder'
import { ResetPasswordPage } from '@/pages/reset-password'
import { ProtectedRoute } from '@/routes/protected'

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
        <Route path="/reset-password" element={<ResetPasswordPage />} />

        <Route element={<ProtectedRoute />}>
          <Route element={<AdminShell />}>
            <Route index element={<OverviewPage />} />
            <Route path="account" element={<AccountPage />} />
            <Route path="companies" element={<CompaniesPage />} />
            <Route path="companies/:companyId" element={<CompanyDetailPage />} />
            <Route
              path="users"
              element={
                <PlaceholderPage
                  title="Users"
                  description="Tenant membership, quotas, and role assignment."
                  apiHint="/api/users"
                />
              }
            />
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
            <Route
              path="settings"
              element={
                <PlaceholderPage
                  title="Settings"
                  description="Platform flags, enterprise LLM/SSO, and tenant defaults."
                  apiHint="/api/admin/platform-settings · /api/enterprise/*"
                />
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
