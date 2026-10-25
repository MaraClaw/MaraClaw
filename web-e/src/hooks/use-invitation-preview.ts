import { useEffect, useRef, useState } from 'react'

import { useAuth } from '@/hooks/use-auth'
import { previewInvitation, type InvitationPreview } from '@/lib/invitations-api'
import { userFacingRequestError } from '@/lib/http'

export function useInvitationPreview(initialCode: string) {
  const { owner, isCurrent } = useAuth()
  const [code, setCode] = useState(initialCode)
  const [preview, setPreview] = useState<InvitationPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  function edit(value: string) {
    request.current?.abort()
    setCode(value)
    setPreview(null)
    setError(null)
    setBusy(false)
  }
  async function validate() {
    request.current?.abort()
    if (!isCurrent(owner)) return
    const controller = new AbortController()
    request.current = controller
    setPreview(null)
    setError(null)
    setBusy(true)
    try {
      const result = await previewInvitation(code.trim(), { owner, signal: controller.signal })
      if (!controller.signal.aborted && isCurrent(owner)) setPreview(result)
    } catch (cause) {
      if (!controller.signal.aborted && isCurrent(owner)) setError(userFacingRequestError(cause, 'Unable to validate this invitation. Try again.'))
    } finally {
      if (!controller.signal.aborted && isCurrent(owner)) setBusy(false)
    }
  }
  return { code, preview, error, busy, edit, validate }
}
