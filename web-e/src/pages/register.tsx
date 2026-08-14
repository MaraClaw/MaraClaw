import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { apiRequest, ApiError, setToken } from '@/lib/api'
import type { AuthResponse } from '@/lib/types'

export function RegisterPage() {
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [invitationCode, setInvitationCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setPending(true)
    try {
      const result = await apiRequest<AuthResponse>('/api/auth/register/init', {
        method: 'POST',
        token: null,
        body: {
          email,
          username: username || email.split('@')[0],
          password,
          invitation_code: invitationCode.trim() || undefined,
        },
      })
      setToken(result.access_token)
      if (result.needs_org_confirm) {
        navigate('/join', { state: { suggested: result.suggested_org } })
        return
      }
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Registration failed')
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 p-6">
      <div>
        <h1 className="text-3xl font-semibold">Create your OpenClaw account</h1>
        <p className="mt-2 text-sm text-neutral-600">
          If your work email matches a company, we will ask you to join it. Otherwise you join OpenClaw.
        </p>
      </div>
      <form className="flex flex-col gap-3" onSubmit={onSubmit}>
        <input className="rounded-lg border px-3 py-2" placeholder="Email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <input className="rounded-lg border px-3 py-2" placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} />
        <input className="rounded-lg border px-3 py-2" placeholder="Password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={6} required />
        <input className="rounded-lg border px-3 py-2" placeholder="Invitation code (optional)" value={invitationCode} onChange={(e) => setInvitationCode(e.target.value)} />
        {error ? <p className="text-sm text-red-700">{error}</p> : null}
        <button className="rounded-lg bg-neutral-900 px-3 py-2 text-white" disabled={pending} type="submit">
          {pending ? 'Creating…' : 'Create account'}
        </button>
      </form>
      <p className="text-sm">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </main>
  )
}
