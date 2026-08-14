import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { apiRequest, ApiError, getToken, setToken } from '@/lib/api'
import type { AuthResponse, EmailLookupResponse, SuggestedOrg, UserOut } from '@/lib/types'

export function TransferPage() {
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [invitationCode, setInvitationCode] = useState('')
  const [match, setMatch] = useState<SuggestedOrg | null>(null)
  const [fallback, setFallback] = useState<SuggestedOrg | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  useEffect(() => {
    if (!getToken()) {
      navigate('/login')
      return
    }
    let cancelled = false
    async function load() {
      try {
        const me = await apiRequest<UserOut>('/api/auth/me')
        if (!me.email) return
        const lookup = await apiRequest<EmailLookupResponse>(
          `/api/tenants/lookup-by-email?email=${encodeURIComponent(me.email)}`,
        )
        if (cancelled) return
        setMatch(lookup.match)
        setFallback(lookup.fallback)
      } catch (err) {
        if (!cancelled && err instanceof ApiError && err.status === 401) {
          navigate('/login')
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [navigate])

  async function transfer(body: { password: string; invitation_code?: string; tenant_id?: string }) {
    setError(null)
    setPending(true)
    try {
      const result = await apiRequest<AuthResponse>('/api/tenants/transfer', {
        method: 'POST',
        body,
      })
      if (result.access_token) setToken(result.access_token)
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Transfer failed')
    } finally {
      setPending(false)
    }
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (!getToken()) {
      navigate('/login')
      return
    }
    const code = invitationCode.trim()
    if (!code) {
      setError('Enter an invitation code, or choose a destination below.')
      return
    }
    await transfer({ password, invitation_code: code })
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 p-6">
      <div>
        <h1 className="text-3xl font-semibold">Transfer organization</h1>
        <p className="mt-2 text-sm text-neutral-600">
          Move with an invitation code, or to your email-domain company / OpenClaw. Confirm your password either way.
        </p>
      </div>
      <form className="flex flex-col gap-3" onSubmit={(event) => void onSubmit(event)}>
        <input className="rounded-lg border px-3 py-2" placeholder="Invitation code (optional)" value={invitationCode} onChange={(e) => setInvitationCode(e.target.value)} />
        <input className="rounded-lg border px-3 py-2" placeholder="Current password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {error ? <p className="text-sm text-red-700">{error}</p> : null}
        <button className="rounded-lg bg-neutral-900 px-3 py-2 text-white" disabled={pending} type="submit">
          {pending ? 'Transferring…' : 'Transfer with invite'}
        </button>
      </form>
      <div className="flex flex-col gap-2">
        {match ? (
          <button
            className="rounded-lg border px-3 py-2"
            disabled={pending || !password}
            type="button"
            onClick={() => void transfer({ password, tenant_id: match.id })}
          >
            Transfer to {match.name}
          </button>
        ) : null}
        {fallback ? (
          <button
            className="rounded-lg border px-3 py-2"
            disabled={pending || !password}
            type="button"
            onClick={() => void transfer({ password, tenant_id: fallback.id })}
          >
            Transfer to {fallback.name}
          </button>
        ) : null}
      </div>
      <Link to="/" className="text-sm">
        Back
      </Link>
    </main>
  )
}
