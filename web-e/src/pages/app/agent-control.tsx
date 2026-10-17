import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useEffect, useRef, useState, type MouseEvent } from 'react'
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
  controlScreenshot,
  controlType,
  controlUnlock,
} from '@/lib/control-api'
import { listSessions, type AgentOut } from '@/lib/workspace-api'

export function AgentControlPage() {
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const [params, setParams] = useSearchParams()
  const sessions = useQuery({ queryKey: ['sessions', agent.id], queryFn: () => listSessions(agent.id, 'mine') })
  const [selectedSessionId, setSessionId] = useState(params.get('session') ?? '')
  const sessionId = selectedSessionId || sessions.data?.[0]?.id || ''
  const [envType, setEnvType] = useState(params.get('env') ?? 'browser')
  const [locked, setLocked] = useState(false)
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [domain, setDomain] = useState('')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const dragFrom = useRef<{ x: number; y: number } | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)

  useEffect(() => {
    if (!scope.isCurrent() || !locked || !sessionId) return
    let cancelled = false
    async function tick() {
      if (cancelled || !scope.isCurrent()) return
      try {
        const result = await controlScreenshot(agent.id, sessionId)
        if (!cancelled && scope.isCurrent() && result.screenshot) setScreenshot(result.screenshot)
      } catch (error) {
        if (!cancelled && scope.isCurrent()) throw error
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 2500)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [agent.id, locked, sessionId, scope])

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
    setBusy(true)
    try {
      const result = await controlLock(agent.id, sessionId, envType)
      if (!scope.isCurrent()) return
      if (result.status === 'already_locked') {
        toast.error('Someone else is controlling this session')
        return
      }
      setSessionId(sessionId)
      setLocked(true)
      setParams({ session: sessionId, env: envType })
      const url = await controlCurrentUrl(agent.id, sessionId)
      if (!scope.isCurrent()) return
      if (url.url) {
        try {
          setDomain(new URL(url.url).hostname)
        } catch {
          setDomain(url.url)
        }
      }
      toast.success('Take Control locked. Agent tools pause until you unlock.')
    } catch {
      if (!scope.isCurrent()) return
      toast.error('Unable to lock the session')
    } finally {
      if (scope.isCurrent()) setBusy(false)
    }
  }

  async function unlock() {
    if (!scope.isCurrent()) return
    if (!sessionId) return
    setBusy(true)
    try {
      const result = await controlUnlock(agent.id, sessionId, {
        export_cookies: Boolean(domain),
        platform_hint: domain || undefined,
      })
      if (!scope.isCurrent()) return
      setLocked(false)
      toast.success(
        result.cookies_exported ? `Unlocked. Exported ${result.cookie_count ?? 0} cookies.` : 'Unlocked',
      )
    } catch {
      if (!scope.isCurrent()) return
      toast.error('Unable to unlock')
    } finally {
      if (scope.isCurrent()) setBusy(false)
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
            {(sessions.data ?? []).map((session) => (
              <option key={session.id} value={session.id}>
                {session.title}
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
            onClick={() => scope.isCurrent() && void controlPressKeys(agent.id, sessionId, [key.toLowerCase()]).catch(scope.rethrowCurrent)}
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
            void controlType(agent.id, sessionId, typed).then(scope.effect(() => setTyped(''))).catch(scope.rethrowCurrent)
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
              if (point) void controlClick(agent.id, sessionId, point.x, point.y).catch(scope.rethrowCurrent)
            }}
            onMouseDown={(event) => {
              if (!scope.isCurrent()) return
              dragFrom.current = mapPoint(event)
            }}
            onMouseUp={(event) => {
              if (!scope.isCurrent()) return
              const from = dragFrom.current
              const to = mapPoint(event)
              dragFrom.current = null
              if (!locked || !from || !to) return
              if (Math.hypot(to.x - from.x, to.y - from.y) < 8) return
              void controlDrag(agent.id, sessionId, from, to).catch(scope.rethrowCurrent)
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
