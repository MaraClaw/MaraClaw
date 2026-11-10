import * as Dialog from '@radix-ui/react-dialog'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Info, Loader2, MonitorPlay, Paperclip, Send, X } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
import { LiveReply } from '@/components/chat/live-reply'
import { ChatTranscript } from '@/components/chat/transcript'
import { HistoryControl } from '@/components/chat/history-control'
import { SessionList } from '@/components/chat/session-list'
import { SkillMentionInput } from '@/components/chat/skill-mention-input'
import { Button } from '@/components/ui/button'
import { installedAgentSkillsOptions } from '@/hooks/agent-skills-options'
import { useChatHistory } from '@/hooks/use-chat-history'
import { useChatScroll } from '@/hooks/use-chat-scroll'
import {
  useInfiniteQuery as useWorkspaceInfiniteQuery,
  useMutation as useWorkspaceMutation,
  useQuery as useWorkspaceQuery,
} from '@/hooks/session-workspace'
import { authSession } from '@/lib/auth-session'
import { getSession, listSessionsPage, SESSION_PAGE_SIZE, type SessionListScope } from '@/lib/chat-sessions-api'
import { chatSessionKey, chatSessionsKey, distinctSessions } from '@/lib/chat-sessions-query'
import { createAttachmentSubmission } from '@/lib/chat/attachment-submission'
import { createLiveStreamStore } from '@/lib/chat/live-stream'
import { buildSkillMentionContent } from '@/lib/chat/skill-mentions'
import type { SessionOwner } from '@/lib/auth-session'
import { connectAgentChat, type ChatInbound, type ChatOutbound } from '@/lib/chat/ws-client'
import { acceptSubmission, appendCompletion, mergeServerHistory, type ChatLine as Line } from '@/lib/chat/transcript'
import { createChatSubmissions } from '@/lib/chat/submissions'
import { recoverRejectedTurn } from '@/lib/chat/turn-rejection'
import { isWritableSession } from '@/lib/chat/sessions'
import { ChatEventType, archivedSessionId } from '@/lib/chat/protocol'
import { userFacingRequestError } from '@/lib/http'
import { chatUploadLimit, formatUploadLimit, getUploadPolicy } from '@/lib/upload-policy-api'
import {
  createSession,
  uploadChatFile,
  type AgentOut,
} from '@/lib/workspace-api'

const FORWARDED_INFO = /forwarded to openclaw|waiting for response/i
const THINKING_TIMEOUT_MS = 90_000
// A live turn whose completion frame was lost is released once history ends in a reply and no chunk has arrived for this long.
const LIVE_QUIET_MS = 15_000
const LIVE_QUIET_CHECK_MS = 1_000

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
  const queryClient = useQueryClient()
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
  const [liveStore] = useState(createLiveStreamStore)
  const live = useSyncExternalStore(liveStore.subscribe, liveStore.hasLive)
  const liveTurnId = useSyncExternalStore(liveStore.subscribe, () => liveStore.getSnapshot().turnId)
  const [thinking, setThinking] = useState('')
  const [busy, setBusy] = useState(false)
  const [slow, setSlow] = useState(false)
  const [retryStartedAt, setRetryStartedAt] = useState<number | null>(null)
  const [socketReady, setSocketReady] = useState(0)
  const [socketStopped, setSocketStopped] = useState(false)
  const [archivedId, setArchivedId] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [livePreview, setLivePreview] = useState<{ env?: string; screenshot?: string } | null>(null)
  const sendRef = useRef<((payload: ChatOutbound) => void) | null>(null)
  const queuedSendRef = useRef<{ readonly line: Line; readonly sessionId?: string } | null>(null)
  const threadRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const linesRef = useRef<Line[]>([])
  const rejectedTurnRef = useRef(false)
  const historyCountRef = useRef(0)
  const historyTailRoleRef = useRef<string | undefined>(undefined)
  const lastChunkAtRef = useRef(0)
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

  const listScope: SessionListScope = user?.id === agent.creator_id ? 'all' : 'mine'
  const sessionsQuery = useWorkspaceInfiniteQuery<Awaited<ReturnType<typeof listSessionsPage>>, Error, string | null>({
    queryKey: chatSessionsKey(agent.id, listScope),
    initialPageParam: null,
    queryFn: ({ pageParam, signal }) => listSessionsPage(agent.id, listScope, { cursor: pageParam, limit: SESSION_PAGE_SIZE, owner, signal }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token),
  })
  const loadedSessions = useMemo(() => distinctSessions(sessionsQuery.data?.pages), [sessionsQuery.data])

  const uploadPolicy = useWorkspaceQuery({
    queryKey: ['upload-policy'],
    queryFn: ({ signal }) => getUploadPolicy({ signal, owner }),
    retry: false,
  })
  const upload = useWorkspaceMutation({ mutationFn: (file: File) => uploadChatFile(file, agent.id) })

  const sharedSessionQuery = useQuery({
    queryKey: ['shared-session', agent.id],
    queryFn: () => createSession(agent.id),
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token) && !sessionId,
    refetchOnMount: 'always',
  })
  const activeId = sessionId ?? sharedSessionQuery.data?.id
  const loadedSession = loadedSessions.find((session) => session.id === activeId)
  // A URL-opened session may sit on a page that is not loaded yet: ask the server, which re-checks access.
  const lookupQuery = useWorkspaceQuery({
    queryKey: chatSessionKey(agent.id, sessionId ?? ''),
    queryFn: ({ signal }) => getSession(agent.id, sessionId ?? '', { owner, signal }),
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token) && Boolean(sessionId) && !sessionsQuery.isPending && !loadedSession,
    retry: false,
  })
  const activeSession = loadedSession ?? lookupQuery.data
    ?? (sharedSessionQuery.data?.id === activeId ? sharedSessionQuery.data : undefined)
  const writable = archivedId !== activeId && (!sessionId || isWritableSession(activeSession))
  const installedSkillsQuery = useWorkspaceQuery({
    ...installedAgentSkillsOptions(agent.id),
    enabled: authSession.isCurrent(mountedOwner) && Boolean(token) && writable,
  })
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
      liveStore.reset()
      setThinking('')
      setInfo(null)
      setLivePreview(null)
      setBusy(false)
      setSlow(false)
      setRetryStartedAt(null)
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
  }, [activeId, owner, submissions, sessionId, updateLines, liveStore])

  useEffect(() => {
    if (!isCurrent() || !submissions.active || !sharedSessionQuery.error || activeId || sessionId) return
    const queued = queuedSendRef.current
    if (!queued) return
    queuedSendRef.current = null
    setDraft(queued.line.composerText ?? queued.line.content)
    updateLines([])
    setBusy(false)
  }, [sharedSessionQuery.error, activeId, sessionId, isCurrent, submissions, updateLines])

  const historyQuery = useChatHistory(agent.id, activeId, authSession.isCurrent(mountedOwner) && Boolean(token))
  const threadScroll = useChatScroll(threadRef, {
    scope: `${agent.id}:${activeId ?? ''}`, ready: historyQuery.data !== undefined,
    hasEarlier: historyQuery.hasEarlier, loadingEarlier: historyQuery.loadingEarlier,
    earlierError: historyQuery.earlierError, loadEarlier: historyQuery.loadEarlier,
    retainLatest: historyQuery.retainLatest,
  }, lines)

  useEffect(() => {
    if (!isCurrent(owner) || !historyQuery.data) return
    const firstLoad = historySessionRef.current === undefined
    const switched = !firstLoad && historySessionRef.current !== activeId
    historySessionRef.current = activeId
    historyCountRef.current = historyQuery.data.length
    historyTailRoleRef.current = historyQuery.data.at(-1)?.role
    submissions.reconcile(historyQuery.data)
    const retainedEntries = new Set(historyQuery.data.map((row) => row.entry_id))
    const local = switched ? [] : linesRef.current.filter((row) => !row.fromHistory || (row.entry_id !== undefined && retainedEntries.has(row.entry_id)))
    const next = mergeServerHistory(historyQuery.data, local)
    updateLines(next)
    if (switched || !writable) {
      rejectedTurnRef.current = false
      if (switched) liveStore.reset()
      else liveStore.clear()
      setThinking('')
      setInfo(null)
      setBusy(false)
      setSlow(false)
      return
    }
    if (liveStore.getSnapshot().turnId === null && !next.some((row) => row.pending) && next.at(-1)?.role === 'assistant') {
      setBusy(false)
      setInfo(null)
      setThinking('')
    } else if (!rejectedTurnRef.current && next.at(-1)?.role === 'user' && next.at(-1)?.delivery !== 'rejected') {
      setBusy(true)
    }
  }, [historyQuery.data, activeId, writable, owner, isCurrent, updateLines, submissions, liveStore])

  useEffect(() => {
    if (!isCurrent(owner) || (!busy && !shared) || !activeId) return
    const timer = window.setInterval(() => {
      if (!isCurrent() || !submissions.active) return
      void queryClient.invalidateQueries({ queryKey: ['messages', agent.id, activeId] })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [busy, shared, activeId, agent.id, queryClient, owner, isCurrent, submissions])

  useEffect(() => {
    if (!isCurrent(owner) || liveTurnId === null) return
    const timer = window.setInterval(() => {
      if (!isCurrent() || !submissions.active || liveStore.getSnapshot().turnId !== liveTurnId) return
      const rows = linesRef.current
      if (historyTailRoleRef.current !== 'assistant' || rows.some((row) => row.pending)) return
      if (rows.findLast((row) => row.role === 'user' || row.role === 'assistant')?.role !== 'assistant') return
      if (Date.now() - lastChunkAtRef.current < LIVE_QUIET_MS) return
      liveStore.clear(liveTurnId)
      setThinking('')
      setInfo(null)
      setSlow(false)
      setBusy(false)
    }, LIVE_QUIET_CHECK_MS)
    return () => window.clearInterval(timer)
  }, [liveTurnId, owner, isCurrent, submissions, liveStore])

  useEffect(() => {
    if (!isCurrent(owner) || !busy) return
    const delay = retryStartedAt === null ? THINKING_TIMEOUT_MS : Math.max(0, retryStartedAt + THINKING_TIMEOUT_MS - Date.now())
    const timer = window.setTimeout(() => {
      if (!isCurrent() || !submissions.active) return
      setSlow(true)
      setThinking('')
    }, delay)
    return () => window.clearTimeout(timer)
  }, [busy, owner, isCurrent, submissions, retryStartedAt])

  useEffect(() => {
    if (!isCurrent(owner) || !socketReady) return
    const queued = queuedSendRef.current
    const sendFn = sendRef.current
    if (!queued || queued.sessionId !== activeId || !queued.line.submission || !sendFn || !activeId || historyQuery.isLoading) return
    queuedSendRef.current = null
    updateLines((prev) => prev.includes(queued.line) ? prev : [...prev, queued.line])
    rejectedTurnRef.current = false
    setBusy(true)
    setInfo(null)
    sendFn(queued.line.submission)
  }, [activeId, historyQuery.isLoading, socketReady, owner, isCurrent, updateLines])

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
        const archived = archivedSessionId(event)
        if (archived !== undefined) {
          if (archived !== activeId) return
          setArchivedId(archived)
          setInfo('Archived conversation. Read-only history.')
          setBusy(false)
          setThinking('')
          setSocketStopped(true)
          sendRef.current = null
          queuedSendRef.current = null
          conn.close()
          return
        }
        const textContent = 'content' in event && typeof event.content === 'string' ? event.content : ''
        switch (event.type) {
        case ChatEventType.TURN_ACCEPTED: {
          updateLines((previous) => acceptSubmission(previous, event))
          setBusy(true)
          setInfo(null)
          return
        }
        case ChatEventType.CONNECTED: {
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
        case ChatEventType.CHUNK: {
          if (liveStore.chunk(event)) {
            lastChunkAtRef.current = Date.now()
            setBusy(true)
          }
          return
        }
        case ChatEventType.THINKING: {
          setThinking((prev) => isCurrent() ? prev + textContent : prev)
          return
        }
        case ChatEventType.TOOL_CALL: {
          if (event.live_preview?.screenshot_url) {
            setLivePreview({ env: event.live_preview.env ?? undefined, screenshot: event.live_preview.screenshot_url })
          }
          updateLines((prev) => isCurrent() ? [
            ...prev,
            {
              role: 'tool',
              content: `${event.name ?? 'tool'} · ${event.status ?? ''}${event.result ? `\n${(typeof event.result === 'string' ? event.result : JSON.stringify(event.result)).slice(0, 400)}` : ''}`,
            },
          ] : prev)
          return
        }
        case ChatEventType.AGENTBAY_LIVE: {
          if (event.live_preview?.screenshot_url) {
            setLivePreview({ env: event.env ?? event.live_preview.env ?? undefined, screenshot: event.live_preview.screenshot_url })
          } else if (event.output) {
            setInfo(`AgentBay ${event.env ?? 'code'}: ${event.output.slice(0, 180)}`)
          }
          return
        }
        case ChatEventType.WORKSPACE_DRAFT: {
          setInfo(`Editing ${event.name ?? 'file'}…`)
          return
        }
        case ChatEventType.STATUS: {
          if (textContent === 'thinking') setBusy(true)
          return
        }
        case ChatEventType.INFO: {
          const text = textContent
          if (FORWARDED_INFO.test(text)) {
            setBusy(true)
            return
          }
          setInfo(text || null)
          return
        }
        case ChatEventType.TURN_REJECTED: {
          const recovery = recoverRejectedTurn(linesRef.current, event)
          if (!recovery.matched) {
            if (!('client_submission_id' in event)) setInfo(recovery.notice)
            return
          }
          updateLines(recovery.lines)
          rejectedTurnRef.current = true
          liveStore.clear()
          setThinking('')
          setInfo(null)
          setBusy(false)
          composerRef.current?.focus()
          return
        }
        case ChatEventType.ERROR: {
          setInfo(textContent || 'Chat error')
          setThinking('')
          setBusy(false)
          if (submissions.pending().length) setSlow(true)
          return
        }
        case ChatEventType.DONE: {
          const currentLive = liveStore.getSnapshot()
          const unrelated = event.turn_id != null && currentLive.turnId !== null && event.turn_id !== currentLive.turnId
          const content = textContent || (unrelated ? '' : currentLive.text)
          const role = event.role ?? 'assistant'
          if (content || event.attachments?.length) {
            updateLines((prev) => {
              if (!isCurrent()) return prev
              return appendCompletion(prev, {
                role, content,
                ...(event.message_id == null ? {} : { message_id: event.message_id }),
                ...(event.entry_id == null ? {} : { entry_id: event.entry_id }),
                ...(event.attachments == null ? {} : { attachments: event.attachments }),
              })
            })
          }
          liveStore.clear(event.turn_id)
          if (!unrelated) {
            setThinking('')
            setBusy(false)
          }
          void queryClient.invalidateQueries({ queryKey: ['messages', agent.id, activeId] })
          return
        }
        case ChatEventType.ONBOARDED: {
          sessionStorage.setItem(`maraclaw-onboarding-sent:${agent.id}`, '1')
          void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
          return
        }
        case ChatEventType.TRIGGER_NOTIFICATION:
          return
        default: {
          const unreachable: never = event
          throw new Error(`Unhandled chat event: ${JSON.stringify(unreachable)}`)
        }
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
  }, [agent.id, agent.onboarded_for_me, activeId, writable, token, owner, queryClient, historyQuery.isLoading, historyQuery.isError, isCurrent, updateLines, submissions, liveStore])

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
    const content = buildSkillMentionContent(draft, installedSkillsQuery.data ?? [])
    const submission = Object.freeze({ client_submission_id: crypto.randomUUID(), ...content })
    const pendingLine: Line = { role: 'user', content: content.display_content ?? content.content, composerText: draft, pending: true,
      client_submission_id: submission.client_submission_id, submission, delivery: 'unknown', created_at: new Date().toISOString() }
    setSlow(false)
    setRetryStartedAt(null)
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
    if (!isCurrent() || !writable || !submissions.active || !uploadPolicy.isSuccess) return
    if (busy) return
    const limit = chatUploadLimit(file.name, uploadPolicy.data)
    if (file.size > limit) {
      toast.error(`File exceeds the ${formatUploadLimit(limit)} chat upload limit.`)
      return
    }
    if (!sendRef.current) {
      toast.error('Chat is not connected yet. Try again in a moment.')
      return
    }
    setBusy(true)
    setSlow(false)
    setRetryStartedAt(null)
    const uploadSession = activeSessionRef.current
    try {
      const uploaded = await upload.mutateAsync(file)
      if (!isCurrent() || !submissions.active || !sendRef.current || activeSessionRef.current !== uploadSession) return
      const submission = createAttachmentSubmission(uploaded)
      updateLines((prev) => isCurrent() ? [
        ...prev,
        { role: 'user', content: uploaded.filename, file_name: uploaded.filename, attachments: submission.attachments, pending: true,
          client_submission_id: submission.client_submission_id, submission, delivery: 'unknown', created_at: new Date().toISOString() },
      ] : prev)
      rejectedTurnRef.current = false
      setBusy(true)
      setInfo(null)
      sendRef.current(submission)
    } catch (error) {
      if (!isCurrent() || !submissions.active || activeSessionRef.current !== uploadSession) return
      setBusy(false)
        toast.error(userFacingRequestError(error, 'Upload failed'))
    }
  }

  const retry = useCallback((line: Line) => {
    if (!isCurrent() || !writable || !submissions.active || !line.submission || line.delivery === 'accepted'
      || line.retryable === false || !sendRef.current) return
    updateLines((previous) => previous.map((row) => row === line ? { ...row, delivery: 'unknown', pending: true } : row))
    rejectedTurnRef.current = false
    setBusy(true)
    setSlow(false)
    setRetryStartedAt(Date.now())
    setInfo(null)
    sendRef.current(line.submission)
  }, [isCurrent, writable, submissions, updateLines])

  return (
      <section className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
        {!writable ? <div className="shrink-0 border-b border-border px-4 py-2 text-xs text-muted-foreground">
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
          onScroll={threadScroll.onScroll}
          className="chat-thread min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-y-contain p-4 [overflow-anchor:none]"
          aria-busy={busy}
        >
          <SessionList agentId={agent.id} sessions={loadedSessions} activeId={activeId} hasMore={Boolean(sessionsQuery.hasNextPage)}
            loadingMore={sessionsQuery.isFetchingNextPage} error={sessionsQuery.isFetchNextPageError ? sessionsQuery.error : null}
            onLoadMore={() => { if (isCurrent() && !sessionsQuery.isFetching) void sessionsQuery.fetchNextPage({ cancelRefetch: false }) }} />
          {sessionsQuery.isError && !sessionsQuery.isFetchNextPageError ? <RequestErrorToast error={sessionsQuery.error} message="Could not load conversations. Try again." /> : null}
          {lookupQuery.isError ? <RequestErrorToast error={lookupQuery.error} message="Could not open this conversation. Check your access and try again." /> : null}
          <HistoryControl ready={historyQuery.data !== undefined} loading={Boolean(activeId) && historyQuery.isLoading}
            error={historyQuery.error} hasEarlier={historyQuery.hasEarlier} loadingEarlier={historyQuery.loadingEarlier}
            earlierError={historyQuery.earlierError} onRefresh={() => { if (isCurrent()) void historyQuery.refetch() }}
            onLoadEarlier={() => { if (isCurrent()) threadScroll.loadEarlier() }} />
          {!sessionId && sharedSessionQuery.error ? <div className="space-y-2">
            <RequestErrorToast error={sharedSessionQuery.error} message="Could not open the shared conversation." />
            <Button size="sm" variant="outline" onClick={() => { if (isCurrent()) void sharedSessionQuery.refetch() }}>Retry shared conversation</Button>
          </div> : null}
          <ChatTranscript lines={lines} viewport={threadScroll} agentId={agent.id} slow={slow} socketStopped={socketStopped} onRetry={retry} />
          {thinking ? <p className="text-xs text-muted-foreground italic">{thinking}</p> : null}
          <LiveReply store={liveStore} onRender={threadScroll.updateScroll} />
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
          <SkillMentionInput
            key={agent.id}
            agentName={agent.name}
            textareaRef={composerRef}
            value={draft}
            onValueChange={(value) => { if (isCurrent()) setDraft(value) }}
            onSend={() => { void send() }}
            skillsQuery={installedSkillsQuery}
            isCurrent={isCurrent}
            toolbar={<div className="flex items-center justify-between gap-2 p-2">
              <div className="flex items-center gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  aria-label="Attach a file"
                  aria-describedby="chat-upload-policy"
                  disabled={busy || socketStopped || !uploadPolicy.isSuccess}
                  onChange={(event) => {
                    if (!isCurrent()) return
                    const file = event.target.files?.[0]
                    if (file) void onFile(file)
                    event.target.value = ''
                  }}
                />
                <Button type="button" variant="ghost" size="icon" className="size-11" title="Attach a file"
                  disabled={busy || socketStopped || !uploadPolicy.isSuccess}
                  onClick={() => fileInputRef.current?.click()}>
                  <Paperclip className="size-5" aria-hidden />
                  <span className="sr-only">Attach a file</span>
                </Button>
                <Dialog.Root modal={false}>
                  <Dialog.Trigger asChild>
                    <Button type="button" variant="ghost" size="icon" className="size-11" title="Attachment limits">
                      <Info className="size-5" aria-hidden />
                      <span className="sr-only">Attachment limits</span>
                    </Button>
                  </Dialog.Trigger>
                  <Dialog.Content className="absolute bottom-full left-0 z-30 mb-2 w-72 max-w-[calc(100vw-3rem)] rounded-xl border border-border bg-popover p-3 text-sm text-popover-foreground shadow-elevated outline-none">
                    <div className="flex items-center justify-between gap-2">
                      <Dialog.Title className="font-medium">Attachment limits</Dialog.Title>
                      <Dialog.Close asChild>
                        <Button type="button" variant="ghost" size="icon" className="size-11" aria-label="Close attachment limits">
                          <X className="size-4" aria-hidden />
                        </Button>
                      </Dialog.Close>
                    </div>
                    <Dialog.Description className="mt-1 select-text leading-relaxed">
                      {uploadPolicy.isSuccess
                        ? `Images (by filename): up to ${formatUploadLimit(uploadPolicy.data.chat_image_max_bytes)}; other files: up to ${formatUploadLimit(uploadPolicy.data.chat_file_max_bytes)}.`
                      : uploadPolicy.isError ? 'Upload limits must be confirmed before uploading.' : 'Loading upload limits…'}
                    </Dialog.Description>
                  </Dialog.Content>
                </Dialog.Root>
              </div>
              <Button type="submit" disabled={busy || socketStopped || !draft.trim()}>
                {busy ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : <Send className="size-4" aria-hidden />}
                Send
              </Button>
            </div>}
          />
          <div id="chat-upload-policy" className={uploadPolicy.isSuccess ? 'sr-only' : 'mt-2 text-xs text-muted-foreground'}>
            {uploadPolicy.isError ? <div className="space-y-2">
              <RequestErrorToast error={uploadPolicy.error} message="Could not load upload limits. Uploading is unavailable." />
              <Button type="button" size="sm" variant="outline" className="h-11" disabled={uploadPolicy.isFetching}
                onClick={() => { if (isCurrent()) void uploadPolicy.refetch() }}>Retry upload limits</Button>
            </div> : <p role="status">{uploadPolicy.isSuccess
              ? `Images (by filename): up to ${formatUploadLimit(uploadPolicy.data.chat_image_max_bytes)}; other files: up to ${formatUploadLimit(uploadPolicy.data.chat_file_max_bytes)}.`
              : 'Loading upload limits…'}</p>}
          </div>
        </form> : null}
      </section>
  )
}
