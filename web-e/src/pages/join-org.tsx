import { Building2, Loader2, Sparkles, Users } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { AuthShell } from '@/components/auth/auth-shell'
import { AdmissionPending } from '@/components/auth/admission-pending'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { joinDefaultOrg, joinSuggestedOrg, lookupOrgByEmail } from '@/lib/auth-api'
import { ApiError, formatApiDetail } from '@/lib/http'
import type { SuggestedOrg } from '@/lib/types/auth'
import type { AdmissionSummary } from '@/lib/types/company-admissions'

type JoinState = { suggested?: SuggestedOrg | null }

const highlights = [
  {
    icon: Building2,
    title: 'Email match',
    body: 'A claimed company domain can place you with your teammates.',
  },
  {
    icon: Users,
    title: 'OpenClaw fallback',
    body: 'If nothing matches, continue in the default member organization.',
  },
  {
    icon: Sparkles,
    title: 'One workspace',
    body: 'You can transfer later if you join the wrong place.',
  },
]

export function JoinOrgPage() {
  const { status, user, owner, isCurrent, refreshReplacement } = useAuth()
  const navigate = useNavigate()
  const fromState = (useLocation().state as JoinState | null)?.suggested ?? null
  const formId = useId()
  const [suggested, setSuggested] = useState<SuggestedOrg | null>(fromState)
  const [fallback, setFallback] = useState<SuggestedOrg | null>(null)
  const [needsVerify, setNeedsVerify] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [loading, setLoading] = useState(true)
  const [admission, setAdmission] = useState<AdmissionSummary | null>(null)

  useEffect(() => {
    const expectedOwner = owner
    if (!isCurrent(expectedOwner)) return
    if (status === 'anonymous') {
      navigate('/login', { replace: true })
      return
    }
    if (status !== 'authenticated' || !user) return
    const member = user
    if (member.tenant_id) {
      navigate('/app', { replace: true })
      return
    }

    let cancelled = false
    async function load() {
      try {
        if (member.email_verified === false) setNeedsVerify(true)
        if (member.email) {
          const lookup = await lookupOrgByEmail(member.email, expectedOwner)
          if (cancelled || !isCurrent(expectedOwner)) return
          setSuggested(lookup.match ?? fromState)
          setFallback(lookup.fallback)
        }
      } catch (err) {
        if (!cancelled && isCurrent(expectedOwner) && err instanceof ApiError && err.status === 401) {
          navigate('/login', { replace: true })
        }
      } finally {
        if (!cancelled && isCurrent(expectedOwner)) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [fromState, navigate, status, user, owner, isCurrent])

  async function choose(kind: 'suggested' | 'default') {
    const expectedOwner = owner
    if (!isCurrent(expectedOwner)) return
    setError(null)
    setPending(true)
    try {
      const result =
        kind === 'suggested' && suggested
          ? await joinSuggestedOrg(suggested.id, expectedOwner)
          : await joinDefaultOrg(expectedOwner)
      if (!isCurrent(expectedOwner)) return
      if ('request' in result) {
        setAdmission(result.request)
        return
      }
      const installedOwner = await refreshReplacement(expectedOwner, result.access_token)
      if (!installedOwner || !isCurrent(installedOwner)) return
      toast.success('Organization joined')
      navigate('/app', { replace: true })
    } catch (err) {
      if (!isCurrent(expectedOwner)) return
      setError(
        err instanceof ApiError
          ? (formatApiDetail(err.detail) ?? err.message)
          : 'Could not join organization',
      )
    } finally {
      if (isCurrent(expectedOwner)) setPending(false)
    }
  }

  const fallbackName = fallback?.name ?? 'OpenClaw'

  if (status === 'loading') {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <Loader2 className="size-4 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <AuthShell
      headingId={`${formId}-heading`}
      title="Join an organization"
      description={
        needsVerify
          ? 'Verify your email before joining a company suggested by that address.'
          : suggested
            ? `Your email matches ${suggested.name}. Request admission to that company, or continue in ${fallbackName}.`
            : loading
              ? 'Looking up your organization…'
              : `Continue in ${fallbackName}, the default organization for members.`
      }
      brandTitle="Land in the right workspace"
      brandBody="We match claimed email domains to companies. You stay in control of the final choice."
      highlights={highlights}
    >
      {admission ? <AdmissionPending request={admission} /> : null}
      {error ? (
        <div
          role="alert"
          className="mb-4 rounded-xl border border-destructive/30 bg-destructive/8 px-3.5 py-3 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}

      <div className="flex flex-col gap-2">
        {suggested && !needsVerify ? (
          <Button className="h-11" disabled={pending || admission !== null} onClick={() => void choose('suggested')}>
            {pending ? <Loader2 className="size-4 animate-spin" /> : null}
            Join {suggested.name}
          </Button>
        ) : null}
        <Button
          variant={suggested && !needsVerify ? 'outline' : 'default'}
          className="h-11"
          disabled={pending || loading}
          onClick={() => void choose('default')}
        >
          Continue with {fallbackName}
        </Button>
      </div>
    </AuthShell>
  )
}
