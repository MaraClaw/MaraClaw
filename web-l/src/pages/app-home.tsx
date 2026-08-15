import { Loader2, MessageSquare } from 'lucide-react'
import { Link, Navigate } from 'react-router-dom'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'

export function AppHomePage() {
  const { status, user, logout, needsOrgConfirm } = useAuth()

  if (status === 'loading') {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <Loader2 className="size-4 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (status === 'anonymous') {
    return <Navigate to="/login" replace />
  }

  if (needsOrgConfirm) {
    return <Navigate to="/join" replace />
  }

  return (
    <div className="min-h-svh bg-background">
      <header className="flex items-center justify-between border-b border-border px-5 py-4 md:px-8">
        <Link to="/" className="flex items-center gap-2.5">
          <MaraClawLogo className="size-10" />
          <div>
            <p className="font-display text-sm font-semibold">MaraClaw</p>
            <p className="text-xs text-muted-foreground">Workspace</p>
          </div>
        </Link>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <Button variant="outline" size="sm" asChild>
            <Link to="/transfer">Transfer</Link>
          </Button>
          <Button variant="ghost" size="sm" onClick={logout}>
            Sign out
          </Button>
        </div>
      </header>

      <main className="mx-auto flex min-h-[70svh] max-w-lg flex-col justify-center gap-4 px-6 py-16">
        <span className="flex size-12 items-center justify-center rounded-2xl bg-primary/12 text-primary">
          <MessageSquare className="size-5" aria-hidden />
        </span>
        <h1 className="font-display text-3xl font-semibold tracking-tight">
          Welcome{user?.display_name ? `, ${user.display_name}` : ''}
        </h1>
        <p className="text-sm leading-relaxed text-muted-foreground">
          You are signed in{user?.email ? ` as ${user.email}` : ''}. Chat and the agent workspace
          will live here. Until then you can transfer organizations or return to the product site.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button asChild>
            <Link to="/">Back to MaraClaw</Link>
          </Button>
          <Button variant="outline" asChild>
            <Link to="/transfer">Change organization</Link>
          </Button>
        </div>
      </main>
    </div>
  )
}
