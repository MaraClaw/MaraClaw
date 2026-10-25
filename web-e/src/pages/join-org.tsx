import { Building2, Lock, Users } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { AuthShell } from '@/components/auth/auth-shell'
import { AdmissionPending } from '@/components/auth/admission-pending'
import { InvitationCode } from '@/components/auth/invitation-code'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { useInvitationLink } from '@/hooks/use-invitation-link'
import { useInvitationPreview } from '@/hooks/use-invitation-preview'
import { useJoinedWorkspace } from '@/hooks/use-joined-workspace'
import { joinDefaultOrg } from '@/lib/auth-api'
import { joinInvitation } from '@/lib/invitations-api'
import { userFacingRequestError } from '@/lib/http'
import { userMustChangePassword, type JoinOrgResponse } from '@/lib/types/auth'

const highlights = [
  { icon: Building2, title: 'An invitation comes first', body: 'Validate the code sent to your email to reveal the destination.' },
  { icon: Lock, title: 'Confirm before joining', body: 'A valid invitation joins the company without another approval.' },
  { icon: Users, title: 'No invitation yet?', body: 'Continue in the default member workspace and transfer later.' },
]

export function JoinOrgPage() {
  const { status, user } = useAuth()
  const code = useInvitationLink()
  if (status === 'loading') return <p role="status" className="p-6 text-sm text-muted-foreground">Loading…</p>
  if (status === 'anonymous') return <Navigate to="/login" replace state={{ from: { pathname: code ? `/transfer#code=${encodeURIComponent(code)}` : '/join' } }} />
  if (userMustChangePassword(user)) return <Navigate to="/app/settings" replace />
  if (user?.tenant_id) return <Navigate to="/app" replace />
  return <JoinSession initialCode={code} />
}

function JoinSession({ initialCode }: { readonly initialCode: string }) {
  const { owner } = useAuth()
  const [session, setSession] = useState({ owner, generation: 0 })
  if (session.owner !== owner) { setSession({ owner, generation: session.generation + 1 }); return null }
  return <JoinForm key={session.generation} initialCode={session.generation === 0 ? initialCode : ''} />
}

function JoinForm({ initialCode }: { readonly initialCode: string }) {
  const headingId = useId()
  const { owner, user, isCurrent } = useAuth()
  const invitation = useInvitationPreview(initialCode)
  const enter = useJoinedWorkspace()
  const submitting = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<JoinOrgResponse | null>(null)
  async function choose(invited: boolean) {
    if (!isCurrent(owner) || submitting.current || (invited && !invitation.preview)) return
    submitting.current = true
    setBusy(true)
    setError(null)
    try {
      const joined = result ?? (invited ? await joinInvitation(invitation.code.trim(), owner) : await joinDefaultOrg(owner))
      if (!isCurrent(owner)) return
      setResult(joined)
      await enter(joined)
    } catch (cause) {
      if (isCurrent(owner)) setError(userFacingRequestError(cause, 'Unable to join or open the workspace. Try again.'))
    } finally {
      if (isCurrent(owner)) { submitting.current = false; setBusy(false) }
    }
  }
  return <AuthShell headingId={headingId} title="Join an organization" description="Validate your invitation to see the company, then confirm that you want to join."
    brandTitle="Land in the right workspace" brandBody="Only the company that invited you is shown. No organization directory or domain matching is needed." highlights={highlights}>
    {error ? <p role="alert" className="mb-4 text-sm text-destructive">{error}</p> : null}
    {user?.email_verified === false ? <p className="mb-4 text-sm text-muted-foreground">Verify your email before joining a company.</p> : null}
    {result && 'request' in result && result.status === 'pending' ? <AdmissionPending request={result.request} /> : null}
    <InvitationCode invitation={invitation} disabled={busy || result !== null || user?.email_verified === false} />
    {invitation.preview || result ? <Button className="h-11 w-full" disabled={busy || user?.email_verified === false || (result !== null && 'request' in result && result.status === 'pending')} onClick={() => void choose(Boolean(invitation.preview))}>
      {busy ? 'Opening workspace…' : result ? 'Open joined workspace' : 'Join organization'}
    </Button> : null}
    <Button variant="outline" className="mt-4 h-11 w-full" disabled={busy || result !== null} onClick={() => void choose(false)}>Continue in default workspace</Button>
  </AuthShell>
}
