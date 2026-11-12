import { Toaster } from 'sonner'

import { AuthProvider } from '@/hooks/use-auth'
import { TranslationProvider } from '@/hooks/use-translation'
import { AppRouter } from '@/routes'

export default function App() {
  return (
    <TranslationProvider><AuthProvider>
      <AppRouter />
      <Toaster richColors closeButton position="top-right" />
    </AuthProvider></TranslationProvider>
  )
}
