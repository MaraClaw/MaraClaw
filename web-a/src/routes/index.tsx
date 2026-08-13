import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AdminShell } from '@/components/layout/admin-shell'
import { LoginPage } from '@/pages/login'
import { OverviewPage } from '@/pages/overview'
import { PlaceholderPage } from '@/pages/placeholder'
import { ProtectedRoute } from '@/routes/protected'

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        <Route element={<ProtectedRoute />}>
          <Route element={<AdminShell />}>
            <Route index element={<OverviewPage />} />
            <Route
              path="companies"
              element={
                <PlaceholderPage
                  title="Companies"
                  description="Platform-wide company management and stats."
                  apiHint="/api/admin/companies"
                />
              }
            />
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
