import { useTranslation } from '@/hooks/use-translation'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useRef, useState, type MouseEvent } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
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
import { listSessionsPage } from '@/lib/chat-sessions-api'
import { controlSessionsKey } from '@/lib/chat-sessions-query'
import { createSession, type AgentOut } from '@/lib/workspace-api'
import { controlSessionId, isWritableSession } from '@/lib/chat/sessions'
import { useControlLease } from './use-control-lease'
import { ControlCaptureFields } from './control-capture-fields'

export function AgentControlPage() {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const [params, setParams] = useSearchParams()
  // The first page of the member's own sessions leads with the shared one and holds every session Take Control can drive.
  const sessions = useQuery({
    queryKey: controlSessionsKey(agent.id),
    queryFn: ({ signal }) => listSessionsPage(agent.id, 'mine', { owner: scope.owner, signal }),
  })
  const loadedSessions = sessions.data?.items ?? []
  const sharedSession = useQuery({
    queryKey: ['shared-session', agent.id], queryFn: () => createSession(agent.id),
    enabled: sessions.isSuccess && !loadedSessions.some((session) => session.source_channel === 'shared'),
  })
  const [selectedSessionId, setSessionId] = useState(params.get('session') ?? '')
  const availableSessions = loadedSessions.filter(isWritableSession)
  if (sharedSession.data && !availableSessions.some((session) => session.id === sharedSession.data.id)) availableSessions.push(sharedSession.data)
  const sessionId = controlSessionId(availableSessions, selectedSessionId) ?? ''
  const [envType, setEnvType] = useState(params.get('env') ?? 'browser')
  const control = useControlLease(scope)
  const { lease, screenshot, domain, setDomain, typed, setTyped, busy, capture, setCapture, captureError } = control
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
          toast.error(translate("Someone else is controlling this session"))
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
      toast.success(translate("Take Control locked. Agent tools pause until you unlock."))
    } catch {
      if (!control.isMounted()) return
      toast.error(translate("Unable to lock the session"))
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
      const result = await control.request((owner) => controlUnlock(owner, capture && domain.trim()
        ? { export_cookies: true, platform_hint: domain.trim(), capture }
        : { export_cookies: false }), true)
      if (!result || !control.isMounted()) return
      toast.success(
        result.cookies_exported ? translate("Control released. Saved {count} cookies.", { count: result.cookie_count ?? 0 }) : translate("Control released. No cookies saved."),
      )
    } catch {
      if (!control.isMounted()) return
      toast.error(translate("Unable to unlock"))
    } finally {
      finish()
    }
  }

  return (
    <div className="space-y-4 p-6">
      {sessions.isError ? <RequestErrorToast error={sessions.error} message="Unable to load control sessions. Try again." /> : null}
      {sharedSession.isError ? <RequestErrorToast error={sharedSession.error} message="Unable to open the shared control session. Try again." /> : null}
      <div>
        <h2 className="font-display text-lg font-semibold">{translate("Take Control")}</h2>
        <p className="text-sm text-muted-foreground">
          {translate("Drive the live AgentBay browser or desktop. The agent waits while you are locked in.")}</p>
      </div>
      {captureError ? <RequestErrorToast message={captureError} /> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-xs">
          <span className="text-muted-foreground">{translate("Chat session")}</span>
          <Select
            className="mt-1"
            value={sessionId}
            onChange={scope.effect((event) => setSessionId(event.target.value))}
            disabled={locked || busy}
          >
            {availableSessions.map((session) => (
              <option key={session.id} value={session.id}>
                {session.source_channel === 'shared' ? translate("Shared conversation") : session.title}
              </option>
            ))}
          </Select>
        </label>
        <label className="text-xs">
          <span className="text-muted-foreground">{translate("Environment")}</span>
          <Select
            className="mt-1"
            value={envType}
            onChange={scope.effect((event) => setEnvType(event.target.value))}
            disabled={locked || busy}
          >
            <option value="browser">{translate("Browser")}</option>
            <option value="computer">{translate("Computer")}</option>
            <option value="code">{translate("Code")}</option>
          </Select>
        </label>
      </div>
      <ControlCaptureFields agentId={agent.id} canManage={agent.access_level === 'manage'}
        capture={capture} onCapture={setCapture} domain={domain} onDomain={setDomain} busy={busy} />
      <div className="flex flex-wrap gap-2">
        {locked ? (
          <Button size="sm" onClick={() => void unlock()} disabled={busy}>
            {translate("Release control")}</Button>
        ) : (
          <Button size="sm" onClick={() => void lock()} disabled={busy || !sessionId}>
            {translate("Take control")}</Button>
        )}
        {[translate("Enter"), translate("Tab"), translate("Escape"), translate("Backspace")].map((key) => (
          <Button
            key={key}
            size="sm"
            variant="outline"
            disabled={!locked || busy}
            onClick={() => void control.request((owner) => controlPressKeys(owner, [key.toLowerCase()]))}
          >
            {key}
          </Button>
        ))}
      </div>
      <div className="flex gap-2">
        <Input
          value={typed}
          aria-label={translate("Remote session text")}
          onChange={scope.effect((event) => setTyped(event.target.value))}
          placeholder={translate("Type into the remote session")}
          disabled={!locked || busy}
        />
        <Button
          size="sm"
          disabled={!locked || busy || !typed}
          onClick={() => {
            if (!scope.isCurrent()) return
            void control.request((owner) => controlType(owner, typed)).then((result) => {
              if (result && lease && control.isCurrent(lease)) setTyped('')
            })
          }}
        >
          {translate("Send")}</Button>
      </div>
      <div className="overflow-hidden rounded-2xl border border-border bg-muted/30">
        {screenshot ? (
          <img
            ref={imgRef}
            src={screenshot.startsWith('data:') ? screenshot : `data:image/jpeg;base64,${screenshot}`}
            alt={translate("Live AgentBay session")}
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
            {locked ? translate("Waiting for a screenshot…") : translate("Lock the session to start a live preview.")}
          </p>
        )}
      </div>
      <Label className="text-xs text-muted-foreground">
        {translate("Click to click. Drag for sliders. Cookies are saved only with a domain and an explicit save choice.")}</Label>
    </div>
  )
}
