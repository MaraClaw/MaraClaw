import { Component, type ReactNode } from 'react'
import { Toaster } from 'sonner'

import { AuthProvider } from '@/hooks/use-auth'
import { AppRouter } from '@/routes'

class RootErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-svh flex-col items-center justify-center gap-3 bg-background px-6 text-center">
          <p className="font-display text-lg font-semibold">Something went wrong</p>
          <p className="max-w-md text-sm text-muted-foreground">{this.state.error.message}</p>
          <button
            type="button"
            className="rounded-xl bg-primary px-4 py-2 text-sm text-primary-foreground"
            onClick={() => this.setState({ error: null })}
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return (
    <RootErrorBoundary>
      <AuthProvider>
        <AppRouter />
        <Toaster richColors closeButton position="top-right" />
      </AuthProvider>
    </RootErrorBoundary>
  )
}
