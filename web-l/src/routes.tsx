import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppHomePage } from '@/pages/app-home'
import { JoinOrgPage } from '@/pages/join-org'
import { LandingPage } from '@/pages/landing'
import { LoginPage } from '@/pages/login'
import { RegisterPage } from '@/pages/register'
import { TransferPage } from '@/pages/transfer'

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/join" element={<JoinOrgPage />} />
        <Route path="/transfer" element={<TransferPage />} />
        <Route path="/app" element={<AppHomePage />} />
      </Routes>
    </BrowserRouter>
  )
}
