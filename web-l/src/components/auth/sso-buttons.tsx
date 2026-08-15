import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { authorizeProvider, listAuthProviders } from '@/lib/auth-api'
import { SSO_PROVIDER_KEY, SSO_STATE_KEY } from '@/lib/sso-storage'
import type { AuthProvider } from '@/lib/types/auth'

export function SsoButtons() {
  const [providers, setProviders] = useState<AuthProvider[]>([])

  useEffect(() => {
    void listAuthProviders()
      .then((rows) => setProviders(rows.filter((row) => row.is_active)))
      .catch(() => setProviders([]))
  }, [])

  if (providers.length === 0) return null

  async function start(provider: AuthProvider) {
    const state = crypto.randomUUID()
    sessionStorage.setItem(SSO_STATE_KEY, state)
    sessionStorage.setItem(SSO_PROVIDER_KEY, provider.provider_type)
    const redirectUri = `${window.location.origin}/sso/callback`
    const { authorization_url } = await authorizeProvider(provider.provider_type, redirectUri, state)
    window.location.assign(authorization_url)
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
