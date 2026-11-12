import { useTranslation } from '@/hooks/use-translation'
import { Building2, Lock, Users } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { AuthShell } from '@/components/auth/auth-shell'
import { AdmissionPending } from '@/components/auth/admission-pending'
import { InvitationCode } from '@/components/auth/invitation-code'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { useInvitationLink } from '@/hooks/use-invitation-link'
import { useInvitationPreview } from '@/hooks/use-invitation-preview'
import { useJoinedWorkspace } from '@/hooks/use-joined-workspace'
import { hasPendingAdmission } from '@/lib/admission-access'
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
  const { t: translate } = useTranslation()
  const { status, user } = useAuth()
  const code = useInvitationLink()
  if (status === 'loading') return <p role="status" className="p-6 text-sm text-muted-foreground">{translate("Loading…")}</p>
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
  const { t: translate } = useTranslation()
  const headingId = useId()
  const { owner, user, isCurrent } = useAuth()
  const invitation = useInvitationPreview(initialCode)
  const enter = useJoinedWorkspace()
  const submitting = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<JoinOrgResponse | null>(null)
  const [pendingAdmission, setPendingAdmission] = useState<boolean | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    void hasPendingAdmission(null, { owner, signal: controller.signal })
      .then((pending) => {
        if (!controller.signal.aborted && isCurrent(owner)) setPendingAdmission(pending)
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted && isCurrent(owner)) setError(userFacingRequestError(cause, translate("Unable to check admission status. Refresh to try again.")))
      })
    return () => controller.abort()
  }, [owner, isCurrent, translate])
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
      if (isCurrent(owner)) setError(userFacingRequestError(cause, translate("Unable to join or open the workspace. Try again.")))
    } finally {
      if (isCurrent(owner)) { submitting.current = false; setBusy(false) }
    }
  }
  if (pendingAdmission === true) return <Navigate to="/app/notifications?category=admissions" replace />
  if (pendingAdmission === null) return error
    ? <div className="space-y-3 p-6"><RequestErrorToast message={error} /><Button variant="outline" onClick={() => window.location.reload()}>{translate("Retry admission status")}</Button></div>
    : <p role="status" className="p-6 text-sm text-muted-foreground">{translate("Checking admission status…")}</p>
  return <AuthShell headingId={headingId} title={translate("Join an organization")} description={translate("Validate your invitation to see the company, then confirm that you want to join.")}
    brandTitle={translate("Land in the right workspace")} brandBody={translate("Only the company that invited you is shown. No organization directory or domain matching is needed.")} highlights={highlights}>
    {error ? <RequestErrorToast message={error} /> : null}
    {user?.email_verified === false ? <p className="mb-4 text-sm text-muted-foreground">{translate("Verify your email before joining a company.")}</p> : null}
    {result && 'request' in result && result.status === 'pending' ? <AdmissionPending request={result.request} /> : null}
    <InvitationCode invitation={invitation} disabled={busy || result !== null || user?.email_verified === false} />
    {invitation.preview || result ? <Button className="h-11 w-full" disabled={busy || user?.email_verified === false || (result !== null && 'request' in result && result.status === 'pending')} onClick={() => void choose(Boolean(invitation.preview))}>
      {busy ? translate("Opening workspace…") : result ? translate("Open joined workspace") : translate("Join organization")}
    </Button> : null}
    <Button variant="outline" className="mt-4 h-11 w-full" disabled={busy || result !== null} onClick={() => void choose(false)}>{translate("Continue in default workspace")}</Button>
  </AuthShell>
}
