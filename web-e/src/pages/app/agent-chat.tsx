import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, MonitorPlay, Send } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { Link, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { ChatMarkdown } from '@/components/chat/markdown'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { authSession } from '@/lib/auth-session'
import type { SessionOwner } from '@/lib/auth-session'
import { connectAgentChat, isTurnAccepted, isTurnRejected, type ChatInbound, type ChatOutbound } from '@/lib/chat/ws-client'
import { acceptSubmission, appendCompletion, mergeServerHistory, type ChatLine as Line } from '@/lib/chat/transcript'
import { createChatSubmissions, type ChatSubmissions } from '@/lib/chat/submissions'
import { recoverRejectedTurn } from '@/lib/chat/turn-rejection'
import { isWritableSession } from '@/lib/chat/sessions'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  createSession,
  deleteSession,
  listSessionMessages,
  listSessions,
  renameSession,
  uploadChatFile,
  type AgentOut,
} from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const FORWARDED_INFO = /forwarded to openclaw|waiting for response/i
const THINKING_TIMEOUT_MS = 90_000

function ThinkingStatus() {
  return (
    <p
      className="flex items-center gap-2 text-sm text-muted-foreground duration-200 ease-out animate-in fade-in motion-reduce:animate-none"
      role="status"
      aria-live="polite"
    >
      <span className="inline-flex items-center gap-1" aria-hidden>
        <span className="thinking-dot size-1.5 rounded-full bg-muted-foreground" />
        <span className="thinking-dot size-1.5 rounded-full bg-muted-foreground [animation-delay:160ms]" />
        <span className="thinking-dot size-1.5 rounded-full bg-muted-foreground [animation-delay:320ms]" />
      </span>
      Thinking
    </p>
  )
}

// allow: SIZE_OK — Existing route composition retains its view; scoped receipt state lives in lib/chat.
export function AgentChatPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  type MutationOrigin = {
    readonly owner: SessionOwner; readonly queryClient: typeof queryClient; readonly agentId: string
    readonly scope: ChatSubmissions
  }
  const { token, user, owner } = useSyncExternalStore(authSession.subscribe, authSession.getSnapshot)
  const [mountedOwner] = useState(() => owner)
  const mountedRef = useRef(false)
  const isCurrent = useCallback((candidate: SessionOwner = owner) =>
    mountedRef.current && candidate === mountedOwner && authSession.isCurrent(candidate), [owner, mountedOwner])
  useLayoutEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  const [draft, setDraft] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [live, setLive] = useState('')
  const [thinking, setThinking] = useState('')
  const [busy, setBusy] = useState(false)
  const [slow, setSlow] = useState(false)
  const [retryVersion, setRetryVersion] = useState(0)
  const [socketReady, setSocketReady] = useState(0)
  const [socketStopped, setSocketStopped] = useState(false)
  const [info, setInfo] = useState<string | null>(null)
  const [livePreview, setLivePreview] = useState<{ env?: string; screenshot?: string } | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const sendRef = useRef<((payload: ChatOutbound) => void) | null>(null)
  const queuedSendRef = useRef<{ readonly line: Line; readonly sessionId?: string } | null>(null)
  const liveRef = useRef('')
  const threadRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)
  const linesRef = useRef<Line[]>([])
  const rejectedTurnRef = useRef(false)
  const historyCountRef = useRef(0)
  const historySessionRef = useRef<string | undefined>(undefined)
  const updateLines = useCallback((update: Line[] | ((previous: Line[]) => Line[])) => {
    const next = typeof update === 'function' ? update(linesRef.current) : update
    linesRef.current = next
    setLines(next)
  }, [])

  useLayoutEffect(() => authSession.subscribe(() => {
    if (authSession.isCurrent(owner)) return
    sendRef.current = null
    queuedSendRef.current = null
  }), [owner])

  const sessionsQuery = useQuery({
    queryKey: ['sessions', agent.id],
    queryFn: () => listSessions(agent.id, user?.id === agent.creator_id ? 'all' : 'mine'),
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token),
  })

  const sharedSessionQuery = useQuery({
    queryKey: ['shared-session', agent.id],
    queryFn: () => createSession(agent.id),
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token) && !sessionId,
    refetchOnMount: 'always',
  })
  const activeId = sessionId ?? sharedSessionQuery.data?.id
  const activeSession = sessionsQuery.data?.find((session) => session.id === activeId)
    ?? (sharedSessionQuery.data?.id === activeId ? sharedSessionQuery.data : undefined)
  const writable = !sessionId || isWritableSession(activeSession)
  const shared = activeSession?.source_channel === 'shared'
  const submissions = useMemo(() => createChatSubmissions(owner, agent.id, activeId), [owner, agent.id, activeId])
  const submissionScopeRef = useRef(submissions)
  const activeSessionRef = useRef(activeId)
  useLayoutEffect(() => {
    if (submissionScopeRef.current !== submissions) submissionScopeRef.current.retire()
    submissionScopeRef.current = submissions
    const queued = queuedSendRef.current
    if (queued && queued.sessionId === undefined && activeSessionRef.current === undefined && !sessionId && activeId) {
      queuedSendRef.current = { ...queued, sessionId: activeId }
    }
    if (activeSessionRef.current !== activeId && (activeSessionRef.current !== undefined || sessionId)) {
      updateLines([])
      historyCountRef.current = 0
      rejectedTurnRef.current = false
      liveRef.current = ''
      setLive('')
      setThinking('')
      setInfo(null)
      setLivePreview(null)
      setBusy(false)
      setSlow(false)
    }
    activeSessionRef.current = activeId
    if (queuedSendRef.current?.sessionId !== activeId) queuedSendRef.current = null
    const unsubscribe = authSession.subscribe(() => {
      if (!authSession.isCurrent(owner)) submissions.retire()
    })
    return () => {
      unsubscribe()
      queueMicrotask(() => {
        if (!mountedRef.current || submissionScopeRef.current !== submissions) submissions.retire()
      })
    }
  }, [activeId, owner, submissions, sessionId, updateLines])

  useEffect(() => {
    if (!isCurrent() || !submissions.active || !sharedSessionQuery.error || activeId || sessionId) return
    const queued = queuedSendRef.current
    if (!queued) return
    queuedSendRef.current = null
    setDraft(queued.line.composerText ?? queued.line.content)
    updateLines([])
    setBusy(false)
  }, [sharedSessionQuery.error, activeId, sessionId, isCurrent, submissions, updateLines])

  const historyQuery = useQuery({
    queryKey: ['messages', agent.id, activeId],
    queryFn: () => activeId ? listSessionMessages(agent.id, activeId) : [],
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token && activeId),
    refetchOnMount: 'always',
  })

  useEffect(() => {
    if (!isCurrent() || !historyQuery.data) return
    const firstLoad = historySessionRef.current === undefined
    const switched = !firstLoad && historySessionRef.current !== activeId
    historySessionRef.current = activeId
    historyCountRef.current = historyQuery.data.length
    submissions.reconcile(historyQuery.data)
    const next = mergeServerHistory(historyQuery.data, switched ? [] : linesRef.current)
    updateLines(next)
    if (switched || !writable) {
      rejectedTurnRef.current = false
      liveRef.current = ''
      setLive('')
      setThinking('')
      setInfo(null)
      setBusy(false)
      setSlow(false)
      return
    }
    if (!next.some((row) => row.pending) && next.at(-1)?.role === 'assistant') {
      setBusy(false)
      setInfo(null)
      setThinking('')
    } else if (!rejectedTurnRef.current && next.at(-1)?.role === 'user' && next.at(-1)?.delivery !== 'rejected') {
      setBusy(true)
    }
  }, [historyQuery.data, activeId, writable, owner, isCurrent, updateLines, submissions])

  useEffect(() => {
    if (!isCurrent() || (!busy && !shared) || !activeId) return
    const timer = window.setInterval(() => {
      if (!isCurrent() || !submissions.active) return
      void queryClient.invalidateQueries({ queryKey: ['messages', agent.id, activeId] })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [busy, shared, activeId, agent.id, queryClient, owner, isCurrent, submissions])

  useEffect(() => {
    if (!isCurrent() || !busy) return
    const timer = window.setTimeout(() => {
      if (!isCurrent() || !submissions.active) return
      setSlow(true)
      setThinking('')
    }, THINKING_TIMEOUT_MS)
    return () => window.clearTimeout(timer)
  }, [busy, owner, isCurrent, submissions, retryVersion])

  useEffect(() => {
    if (!isCurrent()) return
    const queued = queuedSendRef.current
    const sendFn = sendRef.current
    if (!queued || queued.sessionId !== activeId || !queued.line.submission || !sendFn || !activeId || historyQuery.isLoading) return
    queuedSendRef.current = null
    updateLines((prev) => prev.includes(queued.line) ? prev : [...prev, queued.line])
    rejectedTurnRef.current = false
    setBusy(true)
    setInfo(null)
    sendFn(queued.line.submission)
  }, [activeId, historyQuery.isLoading, historyQuery.data, socketReady, owner, isCurrent, updateLines])

  useEffect(() => {
    if (!isCurrent()) return
    const thread = threadRef.current
    if (!thread) return
    const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80
    if (nearBottom) thread.scrollTop = thread.scrollHeight
  }, [lines, live, thinking, info, owner, isCurrent])

  useEffect(() => {
    if (!isCurrent() || !token || !activeId || !writable || historyQuery.isLoading || historyQuery.isError) return
    let greeted = false
    let active = true
    const current = () => active && isCurrent() && submissions.active && activeSessionRef.current === activeId
    const conn = connectAgentChat(agent.id, activeId, {
      owner, submissions,
      onReconnect() {
        if (!current()) return
        void queryClient.invalidateQueries({ queryKey: ['messages', agent.id, activeId] })
      },
      onEvent(event: ChatInbound) {
        if (!current()) return
        if (isTurnAccepted(event)) {
          updateLines((previous) => acceptSubmission(previous, event))
          setBusy(true)
          setInfo(null)
          return
        }
        if (event.type === 'connected') {
          const sentKey = `maraclaw-onboarding-sent:${agent.id}`
          const alreadySent =
            typeof sessionStorage !== 'undefined' && sessionStorage.getItem(sentKey) === '1'
          if (agent.onboarded_for_me === false && !greeted && !alreadySent && historyCountRef.current === 0) {
            greeted = true
            sessionStorage.setItem(sentKey, '1')
            conn.send({ kind: 'onboarding_trigger' })
          }
          return
        }
        if (event.type === 'chunk') {
          liveRef.current += event.content ?? ''
          setLive(liveRef.current)
          setBusy(true)
          return
        }
        if (event.type === 'thinking') {
          setThinking((prev) => isCurrent() ? prev + (event.content ?? '') : prev)
          return
        }
        if (event.type === 'tool_call') {
          if (event.live_preview?.screenshot_url) {
            setLivePreview({ env: event.live_preview.env, screenshot: event.live_preview.screenshot_url })
          }
          updateLines((prev) => isCurrent() ? [
            ...prev,
            {
              role: 'tool',
              content: `${event.name ?? 'tool'} · ${event.status ?? ''}${event.result ? `\n${String(event.result).slice(0, 400)}` : ''}`,
            },
          ] : prev)
          return
        }
        if (event.type === 'agentbay_live') {
          if (event.live_preview?.screenshot_url) {
            setLivePreview({ env: event.env ?? event.live_preview.env, screenshot: event.live_preview.screenshot_url })
          } else if (event.output) {
            setInfo(`AgentBay ${event.env ?? 'code'}: ${event.output.slice(0, 180)}`)
          }
          return
        }
        if (event.type === 'workspace_draft') {
          setInfo(`Editing ${event.name ?? 'file'}…`)
          return
        }
        if (event.type === 'status' && (event.content ?? '') === 'thinking') {
          setBusy(true)
          return
        }
        if (event.type === 'info') {
          const text = event.content ?? ''
          if (FORWARDED_INFO.test(text)) {
            setBusy(true)
            return
          }
          setInfo(text || null)
          return
        }
        if (isTurnRejected(event)) {
          const recovery = recoverRejectedTurn(linesRef.current, event)
          if (!recovery.matched) {
            if (event.client_submission_id === undefined) setInfo(recovery.notice)
            return
          }
          updateLines(recovery.lines)
          rejectedTurnRef.current = true
          liveRef.current = ''
          setLive('')
          setThinking('')
          setInfo(null)
          setBusy(false)
          composerRef.current?.focus()
          return
        }
        if (event.type === 'error') {
          setInfo(event.content ?? 'Chat error')
          setThinking('')
          setBusy(false)
          if (submissions.pending().length) setSlow(true)
          return
        }
        if (event.type === 'done') {
          const content = event.content || liveRef.current
          const role = event.role ?? 'assistant'
          if (content) {
            updateLines((prev) => {
              if (!isCurrent()) return prev
              return appendCompletion(prev, {
                role, content,
                ...(event.message_id === undefined ? {} : { message_id: event.message_id }),
                ...(event.entry_id === undefined ? {} : { entry_id: event.entry_id }),
              })
            })
          }
          liveRef.current = ''
          setLive('')
          setThinking('')
          setBusy(false)
          void queryClient.invalidateQueries({ queryKey: ['messages', agent.id, activeId] })
          return
        }
        if (event.type === 'onboarded') {
          sessionStorage.setItem(`maraclaw-onboarding-sent:${agent.id}`, '1')
          void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
        }
      },
      onClose(code) {
        if (!current()) return
        if (code === 4001) toast.error('Session expired. Sign in again.')
        if (code === 4003) toast.error('This agent is expired or you cannot use it.')
        if (code === 4001 || code === 4003) {
          if (sendRef.current === conn.send) sendRef.current = null
          setSocketStopped(true)
          setBusy(false)
          setThinking('')
        }
      },
    })
    sendRef.current = conn.send
    queueMicrotask(() => {
      if (!isCurrent() || sendRef.current !== conn.send) return
      setSocketStopped(false)
      setSocketReady((n) => isCurrent() ? n + 1 : n)
    })
    return () => {
      active = false
      if (sendRef.current === conn.send) sendRef.current = null
      conn.close()
    }
  }, [agent.id, agent.onboarded_for_me, activeId, writable, token, owner, queryClient, historyQuery.isLoading, historyQuery.isError, isCurrent, updateLines, submissions])

  const rename = useMutation({
    mutationFn: async (origin: MutationOrigin & { readonly sessionId: string; readonly title: string }) =>
      isCurrent(origin.owner) ? renameSession(origin.agentId, origin.sessionId, origin.title) : null,
    onSuccess(_result, origin) {
      if (!isCurrent(origin.owner)) return
      setRenamingId(null)
      void origin.queryClient.invalidateQueries({ queryKey: ['sessions', origin.agentId] })
    },
  })

  const remove = useMutation({
    mutationFn: async (origin: MutationOrigin & { readonly sessionId: string; readonly active: boolean }) =>
      isCurrent(origin.owner) ? deleteSession(origin.agentId, origin.sessionId) : null,
    onSuccess(_result, origin) {
      if (!isCurrent(origin.owner)) return
      void origin.queryClient.invalidateQueries({ queryKey: ['sessions', origin.agentId] })
      if (origin.active) navigate(`/app/agents/${origin.agentId}/chat`)
    },
  })

  const sessionList = sessionsQuery.data ?? []
  const visible = useMemo(() => lines, [lines])
  const currentTurn = lines.findLast((line) => line.role === 'user' || line.role === 'assistant')
  const accepted = currentTurn?.role === 'user' && (currentTurn.delivery === 'accepted'
    || (currentTurn.fromHistory && Boolean(currentTurn.message_id)))

  async function send() {
    if (!isCurrent() || !writable || !submissions.active) return
    const text = draft.trim()
    if (!text || busy) return
    if (!token) {
      toast.error('Sign in again to send.')
      return
    }
    const submission = Object.freeze({ client_submission_id: crypto.randomUUID(), content: text })
    const pendingLine: Line = { role: 'user', content: text, composerText: draft, pending: true,
      client_submission_id: submission.client_submission_id, submission, delivery: 'unknown' }
    setSlow(false)
    if (!activeId) {
      if (queuedSendRef.current || sharedSessionQuery.isError) return
      queuedSendRef.current = { line: pendingLine }
      rejectedTurnRef.current = false
      setDraft('')
      setBusy(true)
      setInfo(null)
      updateLines([pendingLine])
      return
    }
    if (!sendRef.current) {
      toast.error('Chat is not connected yet. Try again in a moment.')
      return
    }
    setDraft('')
    updateLines((prev) => [...prev, pendingLine])
    rejectedTurnRef.current = false
    setBusy(true)
    setInfo(null)
    sendRef.current(submission)
  }

  async function onFile(file: File) {
    if (!isCurrent() || !writable || !submissions.active) return
    if (busy) return
    if (!sendRef.current) {
      toast.error('Chat is not connected yet. Try again in a moment.')
      return
    }
    setBusy(true)
    setSlow(false)
    try {
      const uploaded = await uploadChatFile(file, agent.id)
      if (!isCurrent() || !submissions.active || !sendRef.current) return
      const extracted = uploaded.extracted_text || `[Uploaded ${uploaded.filename}]`
      const submission = Object.freeze({ client_submission_id: crypto.randomUUID(), content: extracted,
        display_content: uploaded.filename, file_name: uploaded.filename })
      updateLines((prev) => isCurrent() ? [
        ...prev,
        { role: 'user', content: extracted, file_name: uploaded.filename, pending: true,
          client_submission_id: submission.client_submission_id, submission, delivery: 'unknown' },
      ] : prev)
      rejectedTurnRef.current = false
      setBusy(true)
      setInfo(null)
      sendRef.current(submission)
    } catch (error) {
      if (!isCurrent() || !submissions.active) return
      setBusy(false)
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Upload failed')
    }
  }

  function retry(line: Line) {
    if (!isCurrent() || !writable || !submissions.active || !line.submission || line.delivery === 'accepted'
      || line.retryable === false || !sendRef.current) return
    updateLines((previous) => previous.map((row) => row === line ? { ...row, delivery: 'unknown', pending: true } : row))
    rejectedTurnRef.current = false
    setBusy(true)
    setSlow(false)
    setRetryVersion((version) => version + 1)
    setInfo(null)
    sendRef.current(line.submission)
  }

  return (
    <div className="grid h-full min-h-0 overflow-hidden grid-cols-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[220px_minmax(0,1fr)] md:grid-rows-1">
      <aside className="min-h-0 max-h-36 overflow-y-auto p-3 md:max-h-none md:border-r md:border-border">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-xs font-medium text-muted-foreground">Conversations</p>
        </div>
        <ul className="space-y-1">
          {sessionList.map((session) => (
            <li key={session.id} className="rounded-lg hover:bg-muted">
              {renamingId === session.id ? (
                <form
                  className="flex gap-1 p-1"
                  onSubmit={(event) => {
                    event.preventDefault()
                    if (!isCurrent()) return
                    rename.mutate({ owner, queryClient, agentId: agent.id, scope: submissions, sessionId: session.id, title: renameTitle })
                  }}
                >
                  <input
                    className="h-7 min-w-0 flex-1 rounded border border-input bg-transparent px-1 text-xs"
                    value={renameTitle}
                    onChange={(event) => { if (isCurrent()) setRenameTitle(event.target.value) }}
                  />
                </form>
              ) : (
                <div className="flex items-center">
                  <button
                    type="button"
                    onClick={() => {
                      if (isCurrent()) navigate(`/app/agents/${agent.id}/chat/${session.id}`)
                    }}
                    className={cn(
                      'min-w-0 flex-1 truncate px-2 py-1.5 text-left text-xs',
                      session.id === activeId && 'font-medium',
                    )}
                  >
                    {session.source_channel === 'shared' ? 'Shared conversation' : session.title || 'Untitled'}
                  </button>
                  {session.source_channel !== 'shared' ? <>
                  <button
                    type="button"
                    className="px-1 text-[10px] text-muted-foreground"
                    onClick={() => {
                      if (!isCurrent()) return
                      setRenamingId(session.id)
                      setRenameTitle(session.title)
                    }}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="px-1 text-[10px] text-muted-foreground"
                    onClick={() => {
                      if (!isCurrent()) return
                      remove.mutate({ owner, queryClient, agentId: agent.id, scope: submissions, sessionId: session.id, active: session.id === activeId })
                    }}
                  >
                    Del
                  </button>
                  </> : null}
                </div>
              )}
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex h-full min-h-0 flex-col overflow-hidden">
        {shared || !sessionId ? <p className="shrink-0 border-b border-border px-4 py-2 text-xs text-muted-foreground">
          Everyone with access to this agent shares this conversation, including messages from connected channels.
        </p> : !writable ? <div className="shrink-0 border-b border-border px-4 py-2 text-xs text-muted-foreground">
          <p>Archived conversation. Read-only history.</p>
          <Link className="underline underline-offset-4" to={`/app/agents/${agent.id}/chat`}>Return to shared conversation</Link>
        </div> : null}
        {livePreview ? (
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
            <p className="text-xs text-muted-foreground">
              Live {livePreview.env ?? 'browser'} session
            </p>
            <Button size="sm" variant="outline" asChild>
              <Link to={`/app/agents/${agent.id}/control?session=${activeId ?? ''}&env=${livePreview.env ?? 'browser'}`}>
                <MonitorPlay className="size-3.5" />
                Take Control
              </Link>
            </Button>
          </div>
        ) : null}
        <div
          ref={threadRef}
          className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-y-contain p-4"
          aria-busy={busy}
        >
          {historyQuery.error ? <p role="alert" className="text-sm text-destructive">
            {historyQuery.error instanceof ApiError ? formatApiDetail(historyQuery.error.detail) ?? historyQuery.error.message : 'Could not load this conversation.'}
          </p> : null}
          {!sessionId && sharedSessionQuery.error ? <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>Could not open the shared conversation.</p>
            <Button size="sm" variant="outline" onClick={() => { if (isCurrent()) void sharedSessionQuery.refetch() }}>Retry shared conversation</Button>
          </div> : null}
          {visible.map((line, index) => (
            <article
              key={line.entry_id ?? line.client_submission_id ?? `${line.created_at ?? 'row'}-${index}`}
              className={cn(
                'max-w-2xl rounded-2xl px-3.5 py-2.5 text-sm whitespace-pre-wrap',
                line.role === 'user' ? 'ml-auto bg-primary text-primary-foreground' : 'bg-muted',
                line.role === 'tool' && 'bg-card text-muted-foreground',
              )}
            >
              {line.role === 'assistant' ? <ChatMarkdown text={line.content} /> : line.content}
              {line.delivery === 'rejected' || (line.delivery === 'unknown' && slow) ? (
                <div className="mt-2 space-y-2">
                  <p className="text-xs" role="status">{line.delivery === 'rejected' ? line.rejectionNotice : 'Acceptance not confirmed.'}</p>
                  {line.retryable !== false ? <Button type="button" size="sm" variant="outline" disabled={socketStopped} onClick={() => retry(line)}>
                    Retry this message
                  </Button> : null}
                </div>
              ) : null}
            </article>
          ))}
          {thinking ? <p className="text-xs text-muted-foreground italic">{thinking}</p> : null}
          {live ? (
            <article
              className="max-w-2xl rounded-2xl bg-muted px-3.5 py-2.5 text-sm"
              aria-live="polite"
              aria-atomic="false"
            >
              <ChatMarkdown text={live} />
            </article>
          ) : null}
          {busy && !live && !slow ? <ThinkingStatus /> : null}
          <p role="status" className="text-xs text-muted-foreground">
            {busy && !live && slow && accepted
              ? 'Accepted. The reply is taking longer than usual. No need to send again.'
              : busy && !live && accepted ? 'Accepted. Waiting for a reply.'
                : busy && !live && slow && !submissions.pending().length ? 'The reply is taking longer than usual.' : ''}
          </p>
          {info ? <p className="text-xs text-muted-foreground">{info}</p> : null}
        </div>
        {writable ? <form
          className="shrink-0 border-t border-border bg-background p-3"
          onSubmit={(event) => {
            event.preventDefault()
            void send()
          }}
        >
          <Textarea
            ref={composerRef}
            aria-label={`Message ${agent.name}`}
            value={draft}
            onChange={(event) => { if (isCurrent()) setDraft(event.target.value) }}
            placeholder={`Message ${agent.name}…`}
            className="max-h-32"
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void send()
              }
            }}
          />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <input
              type="file"
              className="text-xs"
              aria-label="Attach a file"
              disabled={busy || socketStopped}
              onChange={(event) => {
                if (!isCurrent()) return
                const file = event.target.files?.[0]
                if (file) void onFile(file)
                event.target.value = ''
              }}
            />
            <Button type="submit" disabled={busy || socketStopped || !draft.trim()}>
              {busy ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : <Send className="size-4" />}
              Send
            </Button>
          </div>
        </form> : null}
      </section>
    </div>
  )
}
