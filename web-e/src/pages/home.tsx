import { Link } from 'react-router-dom'

import { getToken, setToken } from '@/lib/api'

export function HomePage() {
  const signedIn = Boolean(getToken())
  return (
    <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center gap-4 p-6">
      <h1 className="text-3xl font-semibold">OpenClaw</h1>
      <p className="text-neutral-600">
        {signedIn
          ? 'You are signed in. Chat and agent workspace will live here.'
          : 'Create an account or sign in to join your organization.'}
      </p>
      <div className="flex gap-3">
        {signedIn ? (
          <>
            <Link className="rounded-lg border px-3 py-2" to="/transfer">
              Transfer organization
            </Link>
            <button className="rounded-lg border px-3 py-2" onClick={() => { setToken(null); window.location.reload() }}>
              Sign out
            </button>
          </>
        ) : (
          <>
            <Link className="rounded-lg bg-neutral-900 px-3 py-2 text-white" to="/register">
              Register
            </Link>
            <Link className="rounded-lg border px-3 py-2" to="/login">
              Sign in
            </Link>
          </>
        )}
      </div>
    </main>
  )
}
