import { onlineManager, useQueryClient } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Toaster } from 'sonner'

import { AuthProvider, useAuth } from '@/hooks/use-auth'
import { type CompanyStats } from '@/lib/companies-api'
import { CompanyDeletion } from '@/pages/company-deletion'
import { CompanyDetailPage } from '@/pages/company-detail'
import '@/index.css'

const company = {
  id: 'alpha', name: 'Alpha Labs', slug: 'alpha', is_active: true, sso_enabled: false,
  sso_domain: null, is_system: false, is_default_end_user_org: false, can_disable: true,
  created_at: null, user_count: 20, active_user_count: 19, inactive_user_count: 1,
  agent_count: 8, org_admin_email: null,
} satisfies CompanyStats

function AuthorizationFixture() {
  const { user, owner, status, refreshUser } = useAuth()
  const queryClient = useQueryClient()
  const detail = new URLSearchParams(window.location.search).has('detail')
  return (
    <>
      <output data-testid="authorization">{status}:{user?.role ?? 'none'}</output>
      <output data-testid="session-generation">{owner.generation}</output>
      <button type="button" onClick={() => { void refreshUser() }}>Refresh authorization</button>
      <button type="button" onClick={() => { void queryClient.invalidateQueries({ queryKey: ['company-deletion-preview'] }) }}>Invalidate preview</button>
      <button type="button" onClick={() => onlineManager.setOnline(false)}>Pause mutations</button>
      <button type="button" onClick={() => onlineManager.setOnline(true)}>Resume mutations</button>
      {detail ? <Routes><Route path="/companies/:companyId" element={<CompanyDetailPage />} /></Routes> : <CompanyDeletion company={company} />}
      <Toaster />
    </>
  )
}

const root = document.getElementById('root')
if (!root) throw new Error('Missing authorization fixture root')
createRoot(root).render(<AuthProvider><BrowserRouter><AuthorizationFixture /></BrowserRouter></AuthProvider>)
