import { Building2, Loader2 } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { memberAuthHighlights } from '@/lib/auth-highlights'
import { bindProvider, oauthCallback } from '@/lib/auth-api'
import { SSO_INTENT_KEY, SSO_PROVIDER_KEY, SSO_STATE_KEY } from '@/lib/sso-storage'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  isMultiTenantResponse,
  isTokenResponse,
  type TenantChoice,
} from '@/lib/types/auth'
import { cn } from '@/lib/utils'

export function SsoCallbackPage() {
  const formId = useId()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { applySession } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [tenants, setTenants] = useState<TenantChoice[] | null>(null)
  const [pendingToken, setPendingToken] = useState<string | null>(null)
  const [provider, setProvider] = useState<string | null>(null)
  const [selecting, setSelecting] = useState<string | null>(null)

  useEffect(() => {
    const storedState = sessionStorage.getItem(SSO_STATE_KEY)
    const storedProvider = sessionStorage.getItem(SSO_PROVIDER_KEY)
    const state = searchParams.get('state')
    const code = searchParams.get('code')
    const queryProvider = searchParams.get('provider')
    const nextProvider = queryProvider || storedProvider

    if (!nextProvider || !code) {
      setError('This sign-in link is missing a provider or authorization code.')
      return
    }
    if (storedState && state && storedState !== state) {
      setError('This sign-in attempt expired. Start again from the login page.')
      return
    }

    setProvider(nextProvider)
    const intent = sessionStorage.getItem(SSO_INTENT_KEY)
    sessionStorage.removeItem(SSO_STATE_KEY)
    sessionStorage.removeItem(SSO_PROVIDER_KEY)
    sessionStorage.removeItem(SSO_INTENT_KEY)

    if (intent === 'bind') {
      void bindProvider(nextProvider, code)
        .then(() => {
          toast.success('Account linked')
          navigate('/app/account', { replace: true })
        })
        .catch((err: unknown) => {
          setError(err instanceof ApiError ? (formatApiDetail(err.detail) ?? err.message) : 'Unable to link this provider.')
        })
      return
    }

    void oauthCallback(nextProvider, { code })
      .then((result) => {
        if (isTokenResponse(result)) {
          applySession(result)
          toast.success('Signed in')
          navigate(result.needs_org_confirm ? '/join' : '/app', { replace: true })
          return
        }
        if (isMultiTenantResponse(result)) {
          setTenants(result.tenants)
          setPendingToken(result.pending_token ?? null)
        }
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? (formatApiDetail(err.detail) ?? err.message) : 'SSO sign-in failed.')
      })
  }, [applySession, navigate, searchParams])

  async function chooseTenant(tenant: TenantChoice) {
    if (!provider || !pendingToken || !tenant.tenant_id) return
    setSelecting(tenant.tenant_id)
    try {
      const result = await oauthCallback(provider, {
        pending_token: pendingToken,
        tenant_id: tenant.tenant_id,
      })
      if (isTokenResponse(result)) {
        applySession(result)
        toast.success(`Signed in to ${tenant.tenant_name}`)
        navigate(result.needs_org_confirm ? '/join' : '/app', { replace: true })
      }
    } catch (err) {
      setError(err instanceof ApiError ? (formatApiDetail(err.detail) ?? err.message) : 'Unable to finish SSO.')
    } finally {
      setSelecting(null)
    }
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title={tenants ? 'Choose organization' : 'Signing in'}
      description={
        tenants
          ? 'This account belongs to more than one company. Select where to continue.'
          : 'Finishing the company sign-in…'
      }
      brandTitle="Continue with your company"
      brandBody="SSO returns you to the member workspace, not the admin console."
      highlights={memberAuthHighlights}
    >
      {error ? (
        <div role="alert" className="mb-4 rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      {tenants ? (
        <ul className="space-y-2">
          {tenants.map((tenant) => {
            const busy = selecting === tenant.tenant_id
            return (
              <li key={tenant.tenant_id ?? tenant.tenant_slug}>
                <button
                  type="button"
                  disabled={Boolean(selecting)}
                  onClick={() => void chooseTenant(tenant)}
                  className={cn(
                    'flex w-full items-center gap-3 rounded-2xl border border-border bg-background px-3.5 py-3 text-left',
                    'hover:border-primary/40 hover:bg-accent/40',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  )}
                >
                  <Building2 className="size-4 text-muted-foreground" aria-hidden />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{tenant.tenant_name}</span>
                    {tenant.tenant_slug ? (
                      <span className="block truncate text-xs text-muted-foreground">{tenant.tenant_slug}</span>
                    ) : null}
                  </span>
                  {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                </button>
              </li>
            )
          })}
        </ul>
      ) : error ? (
        <Button asChild className="h-11 w-full">
          <Link to="/login">Back to sign in</Link>
        </Button>
      ) : (
        <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Completing sign-in…
        </div>
      )}
    </AuthShell>
  )
}
