import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { HomePage } from '@/pages/home'
import { JoinOrgPage } from '@/pages/join-org'
import { LoginPage } from '@/pages/login'
import { RegisterPage } from '@/pages/register'
import { TransferPage } from '@/pages/transfer'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/join" element={<JoinOrgPage />} />
        <Route path="/transfer" element={<TransferPage />} />
      </Routes>
    </BrowserRouter>
  )
}
