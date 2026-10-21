import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useRef, useState, type MouseEvent } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import {
  controlClick,
  controlCurrentUrl,
  controlDrag,
  controlLock,
  controlPressKeys,
  controlType,
  controlUnlock,
} from '@/lib/control-api'
import { createSession, listSessions, type AgentOut } from '@/lib/workspace-api'
import { controlSessionId, isWritableSession } from '@/lib/chat/sessions'
import { useControlLease } from './use-control-lease'

export function AgentControlPage() {
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const [params, setParams] = useSearchParams()
  const sessions = useQuery({ queryKey: ['sessions', agent.id], queryFn: () => listSessions(agent.id, 'mine') })
  const sharedSession = useQuery({
    queryKey: ['shared-session', agent.id], queryFn: () => createSession(agent.id),
    enabled: sessions.isSuccess && !sessions.data.some((session) => session.source_channel === 'shared'),
  })
  const [selectedSessionId, setSessionId] = useState(params.get('session') ?? '')
  const availableSessions = (sessions.data ?? []).filter(isWritableSession)
  if (sharedSession.data && !availableSessions.some((session) => session.id === sharedSession.data.id)) availableSessions.push(sharedSession.data)
  const sessionId = controlSessionId(availableSessions, selectedSessionId) ?? ''
  const [envType, setEnvType] = useState(params.get('env') ?? 'browser')
  const control = useControlLease(scope)
  const { lease, screenshot, domain, setDomain, typed, setTyped, busy } = control
  const locked = lease !== null
  const imgRef = useRef<HTMLImageElement | null>(null)

  function mapPoint(event: MouseEvent<HTMLImageElement>) {
    const img = imgRef.current
    if (!img) return null
    const rect = img.getBoundingClientRect()
    const x = Math.round(((event.clientX - rect.left) / rect.width) * img.naturalWidth)
    const y = Math.round(((event.clientY - rect.top) / rect.height) * img.naturalHeight)
    return { x, y }
  }

  async function lock() {
    if (!scope.isCurrent()) return
    if (!sessionId) return
    const finish = control.beginBusy()
    try {
      const result = await controlLock(agent.id, sessionId, envType)
      if (!control.isMounted()) return
      switch (result.status) {
        case 'already_locked':
          toast.error('Someone else is controlling this session')
          return
        case 'locked':
          break
        default:
          result satisfies never
      }
      const captured = control.start({ agentId: agent.id, sessionId, ownerToken: result.owner_token })
      setSessionId(sessionId)
      setParams({ session: sessionId, env: envType })
      const url = await control.request(controlCurrentUrl)
      if (!control.isCurrent(captured)) return
      if (url?.url) {
        try {
          setDomain(new URL(url.url).hostname)
        } catch {
          setDomain(url.url)
        }
      }
      toast.success('Take Control locked. Agent tools pause until you unlock.')
    } catch {
      if (!control.isMounted()) return
      toast.error('Unable to lock the session')
    } finally {
      finish()
    }
  }

  async function unlock() {
    if (!scope.isCurrent()) return
    if (!sessionId) return
    const finish = control.beginBusy()
    try {
      if (!lease) return
      const result = await control.request((owner) => controlUnlock(owner, {
        export_cookies: Boolean(domain),
        platform_hint: domain || undefined,
      }))
      if (!result || !control.isCurrent(lease)) return
      control.end(lease)
      toast.success(
        result.cookies_exported ? `Unlocked. Exported ${result.cookie_count ?? 0} cookies.` : 'Unlocked',
      )
    } catch {
      if (!control.isMounted()) return
      toast.error('Unable to unlock')
    } finally {
      finish()
    }
  }

  return (
    <div className="space-y-4 p-6">
      <div>
        <h2 className="font-display text-lg font-semibold">Take Control</h2>
        <p className="text-sm text-muted-foreground">
          Drive the live AgentBay browser or desktop. The agent waits while you are locked in.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="text-xs">
          <span className="text-muted-foreground">Chat session</span>
          <Select
            className="mt-1"
            value={sessionId}
            onChange={scope.effect((event) => setSessionId(event.target.value))}
            disabled={locked}
          >
            {availableSessions.map((session) => (
              <option key={session.id} value={session.id}>
                {session.source_channel === 'shared' ? 'Shared conversation' : session.title}
              </option>
            ))}
          </Select>
        </label>
        <label className="text-xs">
          <span className="text-muted-foreground">Environment</span>
          <Select
            className="mt-1"
            value={envType}
            onChange={scope.effect((event) => setEnvType(event.target.value))}
            disabled={locked}
          >
            <option value="browser">Browser</option>
            <option value="computer">Computer</option>
            <option value="code">Code</option>
          </Select>
        </label>
        <label className="text-xs">
          <span className="text-muted-foreground">Cookie domain on unlock</span>
          <Input className="mt-1 h-10" value={domain} onChange={scope.effect((event) => setDomain(event.target.value))} />
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        {locked ? (
          <Button size="sm" onClick={() => void unlock()} disabled={busy}>
            Release control
          </Button>
        ) : (
          <Button size="sm" onClick={() => void lock()} disabled={busy || !sessionId}>
            Take control
          </Button>
        )}
        {['Enter', 'Tab', 'Escape', 'Backspace'].map((key) => (
          <Button
            key={key}
            size="sm"
            variant="outline"
            disabled={!locked}
            onClick={() => void control.request((owner) => controlPressKeys(owner, [key.toLowerCase()]))}
          >
            {key}
          </Button>
        ))}
      </div>
      <div className="flex gap-2">
        <Input
          value={typed}
          onChange={scope.effect((event) => setTyped(event.target.value))}
          placeholder="Type into the remote session"
          disabled={!locked}
        />
        <Button
          size="sm"
          disabled={!locked || !typed}
          onClick={() => {
            if (!scope.isCurrent()) return
            void control.request((owner) => controlType(owner, typed)).then((result) => {
              if (result && lease && control.isCurrent(lease)) setTyped('')
            })
          }}
        >
          Send
        </Button>
      </div>
      <div className="overflow-hidden rounded-2xl border border-border bg-muted/30">
        {screenshot ? (
          <img
            ref={imgRef}
            src={screenshot.startsWith('data:') ? screenshot : `data:image/jpeg;base64,${screenshot}`}
            alt="Live AgentBay session"
            className="max-h-[70vh] w-full cursor-crosshair object-contain"
            onClick={(event) => {
              if (!scope.isCurrent()) return
              if (!locked) return
              const point = mapPoint(event)
              if (point) void control.request((owner) => controlClick(owner, point))
            }}
            onMouseDown={(event) => {
              if (!scope.isCurrent()) return
              control.startDrag(mapPoint(event))
            }}
            onMouseUp={(event) => {
              if (!scope.isCurrent()) return
              const from = control.finishDrag()
              const to = mapPoint(event)
              if (!locked || !from || !to) return
              if (Math.hypot(to.x - from.x, to.y - from.y) < 8) return
              void control.request((owner) => controlDrag(owner, from, to))
            }}
          />
        ) : (
          <p className="p-10 text-center text-sm text-muted-foreground">
            {locked ? 'Waiting for a screenshot…' : 'Lock the session to start a live preview.'}
          </p>
        )}
      </div>
      <Label className="text-xs text-muted-foreground">
        Click to click. Drag for sliders. Cookies export to the vault when you release with a domain set.
      </Label>
    </div>
  )
}
