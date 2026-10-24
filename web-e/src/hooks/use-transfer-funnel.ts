import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { useAuth } from '@/hooks/use-auth'
import { lookupOrgByEmail, transferOrg } from '@/lib/auth-api'
import { getTransferQuote } from '@/lib/company-transfers-api'
import { userFacingRequestError } from '@/lib/http'
import type { SuggestedOrg } from '@/lib/types/auth'
import type { AdmissionSummary } from '@/lib/types/company-admissions'
import type { TransferInput, TransferQuote, TransferSelection, TransferSummary } from '@/lib/types/company-transfers'

export function useTransferFunnel() {
  const { owner, user, isCurrent, refreshReplacement } = useAuth()
  const navigate = useNavigate()
  const [quote, setQuote] = useState<TransferQuote | null>(null)
  const [quoteError, setQuoteError] = useState<string | null>(null)
  const request = useRef<AbortController | null>(null)
  const [match, setMatch] = useState<SuggestedOrg | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selection, setSelection] = useState<TransferInput | null>(null)
  const [busy, setBusy] = useState(false)
  const submitting = useRef(false)
  const [admission, setAdmission] = useState<AdmissionSummary | null>(null)
  const [payment, setPayment] = useState<TransferSummary | null>(null)
  const load = useCallback(() => {
    request.current?.abort()
    const controller = new AbortController()
    if (!isCurrent(owner)) return
    request.current = controller
    void getTransferQuote({ owner, signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted && isCurrent(owner)) setQuote(value)
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted && isCurrent(owner)) setQuoteError(userFacingRequestError(cause, 'Unable to load transfer options. Try again.'))
    })
    if (user?.email) void lookupOrgByEmail(user.email, owner).then((value) => {
      if (!controller.signal.aborted && isCurrent(owner)) setMatch(value.match?.id === user.tenant_id ? null : value.match)
    }).catch(() => {
      if (!controller.signal.aborted && isCurrent(owner)) setMatch(null)
    })
  }, [owner, user, isCurrent])
  useEffect(() => {
    load()
    return () => request.current?.abort()
  }, [load])

  async function execute(body: TransferInput) {
    if (!isCurrent(owner) || submitting.current || !quote?.source_eligible || admission || payment) return
    submitting.current = true
    setBusy(true)
    setError(null)
    try {
      const result = await transferOrg(body, owner)
      if (!isCurrent(owner)) return
      if ('transfer_request' in result) {
        setPayment(result.transfer_request)
        setSelection(null)
      } else if ('request' in result) {
        setAdmission(result.request)
        setSelection(null)
      } else {
        const installedOwner = await refreshReplacement(owner, result.access_token)
        if (!installedOwner || !isCurrent(installedOwner)) return
        toast.success('Organization updated')
        navigate('/app', { replace: true })
      }
    } catch (cause) {
      if (isCurrent(owner)) setError(userFacingRequestError(cause, 'Transfer failed. Refresh transfer options or check your saved requests before trying again.'))
    } finally {
      if (isCurrent(owner)) { submitting.current = false; setBusy(false) }
    }
  }
  function submit(body: TransferInput) {
    if (!isCurrent(owner) || busy || !quote?.source_eligible || admission || payment) return
    setError(null)
    if (quote.explicit_choice_required) setSelection(body)
    else void execute(body)
  }
  function confirm(choice: TransferSelection) {
    if (selection && isCurrent(owner)) void execute({ ...selection, ...choice })
  }
  return {
    quote, quoteError, match, error, selection, busy, admission, payment, submit, confirm,
    setError: (value: string) => { if (isCurrent(owner)) setError(value) },
    cancel: () => { if (isCurrent(owner) && !busy) { setSelection(null); setError(null) } },
    reload: () => {
      if (!isCurrent(owner)) return
      setQuote(null)
      setQuoteError(null)
      load()
    },
  }
}
