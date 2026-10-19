import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'

import type { SessionQueryScope } from '@/hooks/session-query-scope'
import { controlScreenshot, type ControlLease, type ControlResult } from '@/lib/control-api'
import { ApiError, userFacingRequestError } from '@/lib/http'

export function useControlLease(scope: SessionQueryScope) {
  const [lease, setLease] = useState<ControlLease | null>(null)
  const owner = useRef<ControlLease | null>(null)
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [domain, setDomain] = useState('')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
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
    setBusy(false)
    busyOwner.current = null
    dragFrom.current = null
    if (lost) toast.error('Control ended because the session lock was lost. Take control again to continue.')
  }, [isCurrent])

  function start(next: ControlLease) {
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

  const request = useCallback(async (action: (captured: ControlLease) => Promise<ControlResult>) => {
    const captured = owner.current
    if (!captured || !isCurrent(captured)) return
    try {
      const result = await action(captured)
      if (!isCurrent(captured)) return
      if (result.status === 'lost_lock') {
        end(captured, true)
        return
      }
      return result
    } catch (error) {
      if (!isCurrent(captured)) return
      if (error instanceof ApiError && (error.status === 400 || error.status === 409)) {
        end(captured, true)
        return
      }
      toast.error(userFacingRequestError(error, 'Unable to update the remote session'), { id: 'control-request-error' })
    }
  }, [end, isCurrent])

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
    startDrag, finishDrag, start, end, isMounted, isCurrent, request,
  }
}
