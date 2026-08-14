import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { apiRequest, ApiError, setToken } from '@/lib/api'
import type { AuthResponse } from '@/lib/types'

export function LoginPage() {
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setPending(true)
    try {
      const result = await apiRequest<AuthResponse>('/api/auth/login', {
        method: 'POST',
        token: null,
        body: { login_identifier: email, password },
      })
      if ('requires_tenant_selection' in result) {
        setError('This account still has multiple organizations. Ask an admin to clean it up.')
        return
      }
      setToken(result.access_token)
      if (result.needs_org_confirm) {
        navigate('/join', { state: { suggested: result.suggested_org } })
        return
      }
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Sign in failed')
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 p-6">
      <div>
        <h1 className="text-3xl font-semibold">Sign in</h1>
        <p className="mt-2 text-sm text-neutral-600">Member accounts only. Platform operators use the admin console.</p>
      </div>
      <form className="flex flex-col gap-3" onSubmit={onSubmit}>
        <input className="rounded-lg border px-3 py-2" placeholder="Email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <input className="rounded-lg border px-3 py-2" placeholder="Password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {error ? <p className="text-sm text-red-700">{error}</p> : null}
        <button className="rounded-lg bg-neutral-900 px-3 py-2 text-white" disabled={pending} type="submit">
          {pending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="text-sm">
        New here? <Link to="/register">Create an account</Link>
      </p>
    </main>
  )
}
