import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { apiRequest, ApiError, getToken, setToken } from '@/lib/api'
import type { AuthResponse, EmailLookupResponse, SuggestedOrg, UserOut } from '@/lib/types'

type JoinState = { suggested?: SuggestedOrg | null }

export function JoinOrgPage() {
  const navigate = useNavigate()
  const fromState = (useLocation().state as JoinState | null)?.suggested ?? null
  const [suggested, setSuggested] = useState<SuggestedOrg | null>(fromState)
  const [fallback, setFallback] = useState<SuggestedOrg | null>(null)
  const [needsVerify, setNeedsVerify] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!getToken()) {
      navigate('/login')
      return
    }
    let cancelled = false
    async function load() {
      try {
        const me = await apiRequest<UserOut>('/api/auth/me')
        if (me.email_verified === false) {
          if (!cancelled) setNeedsVerify(true)
        }
        if (me.email) {
          const lookup = await apiRequest<EmailLookupResponse>(
            `/api/tenants/lookup-by-email?email=${encodeURIComponent(me.email)}`,
          )
          if (cancelled) return
          setSuggested(lookup.match ?? fromState)
          setFallback(lookup.fallback)
        }
      } catch (err) {
        if (!cancelled && err instanceof ApiError && err.status === 401) {
          navigate('/login')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [navigate])

  async function choose(path: '/api/tenants/join-suggested' | '/api/tenants/join-default') {
    setError(null)
    setPending(true)
    try {
      const result = await apiRequest<AuthResponse>(path, {
        method: 'POST',
        body: path === '/api/tenants/join-suggested' ? { tenant_id: suggested?.id } : undefined,
      })
      if (result.access_token) setToken(result.access_token)
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not join organization')
    } finally {
      setPending(false)
    }
  }

  const fallbackName = fallback?.name ?? 'OpenClaw'

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 p-6">
      <div>
        <h1 className="text-3xl font-semibold">Join an organization</h1>
        <p className="mt-2 text-sm text-neutral-600">
          {needsVerify
            ? 'Verify your email before joining a company suggested by that address.'
            : suggested
              ? `Your email matches ${suggested.name}. Join that company, or continue in ${fallbackName}.`
              : loading
                ? 'Looking up your organization…'
                : `Continue in ${fallbackName}, the default organization for members.`}
        </p>
      </div>
      {error ? <p className="text-sm text-red-700">{error}</p> : null}
      <div className="flex flex-col gap-2">
        {suggested && !needsVerify ? (
          <button className="rounded-lg bg-neutral-900 px-3 py-2 text-white" disabled={pending} onClick={() => void choose('/api/tenants/join-suggested')}>
            Join {suggested.name}
          </button>
        ) : null}
        <button className="rounded-lg border px-3 py-2" disabled={pending} onClick={() => void choose('/api/tenants/join-default')}>
          Continue with {fallbackName}
        </button>
      </div>
    </main>
  )
}
