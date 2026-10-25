import { useLayoutEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { z } from 'zod'

const continuation = z.object({ invitationCode: z.string().max(256) })

export function useInvitationLink() {
  const location = useLocation()
  const navigate = useNavigate()
  const [code] = useState(() => {
    const state = continuation.safeParse(location.state)
    return new URLSearchParams(location.hash.slice(1)).get('code')?.slice(0, 256) ?? (state.success ? state.data.invitationCode : '')
  })
  useLayoutEffect(() => {
    if (location.hash || continuation.safeParse(location.state).success) navigate(location.pathname, { replace: true, state: null })
  }, [location.hash, location.pathname, location.state, navigate])
  return code
}
