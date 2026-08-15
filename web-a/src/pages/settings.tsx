import { LogOut, Monitor, Moon, Sun } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { useTheme, type Theme } from '@/hooks/use-theme'
import { cn } from '@/lib/utils'

const themeChoices: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
]

export function SettingsPage() {
  const { user, logout } = useAuth()
  const { theme, setTheme } = useTheme()
  const navigate = useNavigate()

  function handleSignOut() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <div className="space-y-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Session and console preferences. Password changes live on Account.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
          <CardDescription>Choose a light, dark, or system theme for this console.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-3 gap-2" role="group" aria-label="Theme">
            {themeChoices.map(({ value, label, icon: Icon }) => {
              const selected = theme === value
              return (
                <Button
                  key={value}
                  type="button"
                  variant={selected ? 'default' : 'outline'}
                  className={cn('h-auto flex-col gap-1.5 py-3', selected && 'shadow-none')}
                  aria-pressed={selected}
                  onClick={() => setTheme(value)}
                >
                  <Icon className="size-4" aria-hidden />
                  {label}
                </Button>
              )
            })}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Session</CardTitle>
          <CardDescription>
            Signed in as {user?.display_name || user?.email || 'this operator'}. Sign out ends this
            browser session.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button type="button" variant="outline" onClick={handleSignOut}>
            <LogOut className="size-4" aria-hidden />
            Sign out
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
