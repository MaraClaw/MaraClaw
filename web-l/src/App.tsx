import { Toaster } from 'sonner'

import { AuthProvider } from '@/hooks/use-auth'
import { AppRouter } from '@/routes'

export default function App() {
  return (
    <AuthProvider>
      <AppRouter />
      <Toaster richColors closeButton position="top-right" />
    </AuthProvider>
  )
}
