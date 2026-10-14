import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { authorizeProvider, listAuthProviders } from '@/lib/auth-api'
import { SSO_PROVIDER_KEY, SSO_STATE_KEY } from '@/lib/sso-storage'
import type { AuthProvider } from '@/lib/types/auth'

export function SsoButtons() {
  const { owner, isCurrent } = useAuth()
  const [providers, setProviders] = useState<AuthProvider[]>([])

  useEffect(() => {
    const expectedOwner = owner
    let cancelled = false
    void listAuthProviders(undefined, expectedOwner)
      .then((rows) => {
        if (!cancelled && isCurrent(expectedOwner)) setProviders(rows.filter((row) => row.is_active))
      })
      .catch(() => {
        if (!cancelled && isCurrent(expectedOwner)) setProviders([])
      })
    return () => { cancelled = true }
  }, [owner, isCurrent])

  if (providers.length === 0) return null

  async function start(provider: AuthProvider) {
    const expectedOwner = owner
    if (!isCurrent(expectedOwner)) return
    const state = crypto.randomUUID()
    const redirectUri = `${window.location.origin}/sso/callback`
    try {
      const { authorization_url } = await authorizeProvider(provider.provider_type, redirectUri, { state, owner: expectedOwner })
      if (!isCurrent(expectedOwner)) return
      sessionStorage.setItem(SSO_STATE_KEY, state)
      sessionStorage.setItem(SSO_PROVIDER_KEY, provider.provider_type)
      window.location.assign(authorization_url)
    } catch (error) {
      if (!isCurrent(expectedOwner)) return
      if (!(error instanceof Error)) throw error
    }
  }

  return (
    <div className="space-y-2">
      <p className="text-center text-xs text-muted-foreground">Or continue with</p>
      <div className="grid gap-2">
        {providers.map((provider) => (
          <Button
            key={provider.id}
            type="button"
            variant="outline"
            className="h-11 w-full"
            onClick={() => void start(provider)}
          >
            {provider.name || provider.provider_type}
          </Button>
        ))}
      </div>
    </div>
  )
}
