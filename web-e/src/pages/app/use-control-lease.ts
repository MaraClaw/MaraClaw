import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'

import type { SessionQueryScope } from '@/hooks/session-query-scope'
import { controlScreenshot, type ControlLease, type ControlResult, type CredentialCapture } from '@/lib/control-api'
import { ApiError, userFacingRequestError } from '@/lib/http'

export function useControlLease(scope: SessionQueryScope) {
  const [lease, setLease] = useState<ControlLease | null>(null)
  const owner = useRef<ControlLease | null>(null)
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [domain, setDomain] = useState('')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [capture, setCapture] = useState<CredentialCapture | null>({ mode: 'create' })
  const [captureError, setCaptureError] = useState('')
  const releasing = useRef<ControlLease | null>(null)
  const busyOwner = useRef<symbol | null>(null)
  const dragFrom = useRef<{ x: number; y: number } | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; owner.current = null }
  }, [])

  const isMounted = useCallback(() => mounted.current && scope.isCurrent(), [scope])

  const isCurrent = useCallback((captured: ControlLease) => (
    isMounted() && owner.current === captured
  ), [isMounted])

  const end = useCallback((captured: ControlLease, lost = false) => {
    if (!isCurrent(captured)) return
    owner.current = null
    setLease(null)
    setScreenshot(null)
    setDomain('')
    setTyped('')
    setCapture({ mode: 'create' })
    setBusy(false)
    busyOwner.current = null
    dragFrom.current = null
    if (lost) toast.error('Control ended because the session lock was lost. Take control again to continue.')
  }, [isCurrent])

  function start(next: ControlLease) {
    setCaptureError('')
    owner.current = next
    setLease(next)
    return next
  }

  function beginBusy() {
    const operation = Symbol()
    busyOwner.current = operation
    setBusy(true)
    return () => {
      if (isMounted() && busyOwner.current === operation) {
        busyOwner.current = null
        setBusy(false)
      }
    }
  }

  const request = useCallback(async (
    action: (captured: ControlLease) => Promise<ControlResult>, release = false,
  ) => {
    const captured = owner.current
    if (!captured || !isCurrent(captured) || releasing.current === captured) return
    if (release) releasing.current = captured
    try {
      const result = await action(captured)
      if (!isCurrent(captured) || (!release && releasing.current === captured)) return
      if (release) {
        end(captured)
        void scope.client.invalidateQueries({ queryKey: ['credentials', captured.agentId] })
      }
      return result
    } catch (error) {
      if (!isCurrent(captured) || (!release && releasing.current === captured)) return
      if (error instanceof ApiError) {
        const code = error.detail && typeof error.detail === 'object' && 'code' in error.detail ? error.detail.code : null
        if (error.status === 409 && code === 'take_control_lost') {
          end(captured, true)
          return
        }
        if (release && ((error.status === 409 && code === 'credential_revision_conflict') || (error.status === 404 && error.detail === 'Credential not found'))) {
          end(captured)
          void scope.client.invalidateQueries({ queryKey: ['credentials', captured.agentId] })
          setCaptureError(error.status === 409
            ? 'Control released. Cookies were not saved because the selected credential changed. Take control again and reselect a credential, or create a new one.'
            : 'Control released. Cookies were not saved because the selected credential is no longer available. Take control again and choose a new credential.')
          return
        }
      }
      toast.error(userFacingRequestError(error, 'Unable to update the remote session'), { id: 'control-request-error' })
    } finally {
      if (release && releasing.current === captured) releasing.current = null
    }
  }, [end, isCurrent, scope])

  useEffect(() => {
    if (!lease) return
    let cancelled = false
    let pending = false
    async function tick() {
      if (cancelled || pending || !lease || !isCurrent(lease)) return
      pending = true
      const result = await request(controlScreenshot)
      pending = false
      if (!cancelled && isCurrent(lease) && result?.screenshot) setScreenshot(result.screenshot)
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 2500)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [lease, isCurrent, request])

  function startDrag(point: { x: number; y: number } | null) { dragFrom.current = point }
  function finishDrag() {
    const point = dragFrom.current
    dragFrom.current = null
    return point
  }

  return {
    lease, screenshot, domain, setDomain, typed, setTyped, busy, beginBusy,
    capture, setCapture, captureError,
    startDrag, finishDrag, start, end, isMounted, isCurrent, request,
  }
}
