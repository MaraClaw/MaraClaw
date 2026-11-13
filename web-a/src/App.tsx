import { Component, type ReactNode } from 'react'
import { Toaster } from 'sonner'

import { Button } from '@/components/ui/button'
import { AuthProvider } from '@/hooks/use-auth'
import { TranslationProvider, useTranslation } from '@/hooks/use-translation'
import { AppRouter } from '@/routes'

function RootError({ error }: { readonly error: Error }) {
  const { t } = useTranslation()
  return (
    <div role="alert" className="flex min-h-svh flex-col items-center justify-center gap-3 bg-background px-6 text-center">
      <p className="font-display text-lg font-semibold">{t('Something went wrong')}</p>
      <p className="max-w-md text-sm text-muted-foreground">{error.message}</p>
      <Button type="button" onClick={() => window.location.reload()}>{t('Try again')}</Button>
    </div>
  )
}

class RootErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    return this.state.error ? <RootError error={this.state.error} /> : this.props.children
  }
}

export default function App() {
  return (
    <TranslationProvider><RootErrorBoundary><AuthProvider>
      <AppRouter />
      <Toaster richColors closeButton position="top-right" />
    </AuthProvider></RootErrorBoundary></TranslationProvider>
  )
}
