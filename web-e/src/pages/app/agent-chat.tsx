import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, MonitorPlay, Plus, Send } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { ChatMarkdown } from '@/components/chat/markdown'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { connectAgentChat, type ChatInbound, type ChatOutbound } from '@/lib/chat/ws-client'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  createSession,
  deleteSession,
  listSessionMessages,
  listSessions,
  renameSession,
  uploadChatFile,
  type AgentOut,
  type ChatMessage,
} from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

type Line = ChatMessage & { pending?: boolean }

const FORWARDED_INFO = /forwarded to openclaw|waiting for response/i
const THINKING_TIMEOUT_MS = 90_000

function sameUserLine(saved: Line, pending: Line): boolean {
  if (saved.role !== 'user') return false
  if (saved.content === pending.content) return true
  return Boolean(pending.file_name && saved.content.startsWith(`[file:${pending.file_name}]`))
}

function mergeServerHistory(server: Line[], local: Line[]): Line[] {
  const serverUsers = server.filter((row) => row.role === 'user')
  const used = new Set<number>()
  const leftoverPending: Line[] = []
  for (const row of local) {
    if (!row.pending || row.role !== 'user') continue
    const match = serverUsers.findIndex((saved, index) => !used.has(index) && sameUserLine(saved, row))
    if (match >= 0) used.add(match)
    else leftoverPending.push(row)
  }
  const leftoverLocal = local.filter(
    (row) =>
      !row.pending &&
      (row.role === 'assistant' || row.role === 'tool') &&
      !server.some((saved) => saved.role === row.role && saved.content === row.content),
  )
  if (!leftoverPending.length && !leftoverLocal.length) return server
  return [...server, ...leftoverPending, ...leftoverLocal]
}

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
      Thinking ...
    </p>
  )
}

export function AgentChatPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { token, user } = useAuth()
  const [draft, setDraft] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [live, setLive] = useState('')
  const [thinking, setThinking] = useState('')
  const [busy, setBusy] = useState(false)
  const [socketReady, setSocketReady] = useState(0)
  const [info, setInfo] = useState<string | null>(null)
  const [livePreview, setLivePreview] = useState<{ env?: string; screenshot?: string } | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const sendRef = useRef<((payload: ChatOutbound) => void) | null>(null)
  const queuedSendRef = useRef<string | null>(null)
  const creatingSessionRef = useRef(false)
  const liveRef = useRef('')
  const threadRef = useRef<HTMLDivElement | null>(null)
  const linesRef = useRef<Line[]>([])
  const historyCountRef = useRef(0)
  const historySessionRef = useRef<string | undefined>(undefined)
  linesRef.current = lines

  const sessionsQuery = useQuery({
    queryKey: ['sessions', agent.id],
    queryFn: () => listSessions(agent.id, user?.id === agent.creator_id ? 'all' : 'mine'),
  })

  const activeId = sessionId ?? sessionsQuery.data?.[0]?.id

  const historyQuery = useQuery({
    queryKey: ['messages', agent.id, activeId],
    queryFn: () => listSessionMessages(agent.id, activeId!),
    enabled: Boolean(activeId),
    refetchOnMount: 'always',
  })

  useEffect(() => {
    if (!historyQuery.data) return
    const firstLoad = historySessionRef.current === undefined
    const switched = !firstLoad && historySessionRef.current !== activeId
    historySessionRef.current = activeId
    historyCountRef.current = historyQuery.data.length
    const next = mergeServerHistory(historyQuery.data, switched ? [] : linesRef.current)
    setLines(next)
    if (switched) {
      liveRef.current = ''
      setLive('')
      setThinking('')
      setInfo(null)
      setBusy(false)
      return
    }
    if (!next.some((row) => row.pending) && next.at(-1)?.role === 'assistant') {
      setBusy(false)
      setInfo(null)
      setThinking('')
    } else if (next.at(-1)?.role === 'user') {
      setBusy(true)
    }
  }, [historyQuery.data, activeId])

  useEffect(() => {
    if (!busy || !activeId) return
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: ['messages', agent.id, activeId] })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [busy, activeId, agent.id, queryClient])

  useEffect(() => {
    if (!busy) return
    const timer = window.setTimeout(() => {
      setBusy(false)
      setThinking('')
      setInfo('No reply yet. You can send again.')
    }, THINKING_TIMEOUT_MS)
    return () => window.clearTimeout(timer)
  }, [busy])

  useEffect(() => {
    const queued = queuedSendRef.current
    const sendFn = sendRef.current
    if (!queued || !sendFn || !activeId || historyQuery.isLoading) return
    queuedSendRef.current = null
    setLines((prev) => [...prev, { role: 'user', content: queued, pending: true }])
    setBusy(true)
    setInfo(null)
    sendFn({ content: queued })
  }, [activeId, historyQuery.isLoading, historyQuery.data, socketReady])

  useEffect(() => {
    const thread = threadRef.current
    if (!thread) return
    const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80
    if (nearBottom) thread.scrollTop = thread.scrollHeight
  }, [lines, live, thinking, info])

  useEffect(() => {
    if (!token || !activeId || historyQuery.isLoading) return
    let greeted = false
    const conn = connectAgentChat(agent.id, token, activeId, {
      onEvent(event: ChatInbound) {
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
          setThinking((prev) => prev + (event.content ?? ''))
          return
        }
        if (event.type === 'tool_call') {
          if (event.live_preview?.screenshot_url) {
            setLivePreview({ env: event.live_preview.env, screenshot: event.live_preview.screenshot_url })
          }
          setLines((prev) => [
            ...prev,
            {
              role: 'tool',
              content: `${event.name ?? 'tool'} · ${event.status ?? ''}${event.result ? `\n${String(event.result).slice(0, 400)}` : ''}`,
            },
          ])
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
        if (event.type === 'error') {
          setInfo(event.content ?? 'Chat error')
          setThinking('')
          setBusy(false)
          return
        }
        if (event.type === 'done') {
          const content = event.content || liveRef.current
          const role = event.role ?? 'assistant'
          if (content) {
            setLines((prev) => {
              const last = prev.at(-1)
              if (last?.role === role && last.content === content) return prev
              return [...prev, { role, content }]
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
        if (code === 4001) toast.error('Session expired. Sign in again.')
        if (code === 4003) toast.error('This agent is expired or you cannot use it.')
        if (code === 4001 || code === 4003) {
          setBusy(false)
          setThinking('')
        }
      },
    })
    sendRef.current = conn.send
    setSocketReady((n) => n + 1)
    return () => {
      sendRef.current = null
      conn.close()
    }
  }, [agent.id, agent.onboarded_for_me, activeId, token, queryClient, historyQuery.isLoading])

  const newSession = useMutation({
    mutationFn: () => createSession(agent.id, 'New chat'),
    onSuccess(session) {
      void queryClient.invalidateQueries({ queryKey: ['sessions', agent.id] })
      navigate(`/app/agents/${agent.id}/chat/${session.id}`)
    },
  })

  const sessionList = sessionsQuery.data ?? []
  const visible = useMemo(() => lines, [lines])

  async function send() {
    const text = draft.trim()
    if (!text || busy) return
    if (!token) {
      toast.error('Sign in again to send.')
      return
    }
    if (!activeId) {
      if (creatingSessionRef.current) return
      creatingSessionRef.current = true
      queuedSendRef.current = text
      setDraft('')
      setBusy(true)
      setInfo(null)
      setLines([{ role: 'user', content: text, pending: true }])
      try {
        const session = await createSession(agent.id, text.slice(0, 48) || 'New chat')
        void queryClient.invalidateQueries({ queryKey: ['sessions', agent.id] })
        navigate(`/app/agents/${agent.id}/chat/${session.id}`)
      } catch (error) {
        queuedSendRef.current = null
        setDraft(text)
        setBusy(false)
        toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Could not start a chat')
      } finally {
        creatingSessionRef.current = false
      }
      return
    }
    if (!sendRef.current) {
      toast.error('Chat is not connected yet. Try again in a moment.')
      return
    }
    setDraft('')
    setLines((prev) => [...prev, { role: 'user', content: text, pending: true }])
    setBusy(true)
    setInfo(null)
    sendRef.current({ content: text })
  }

  async function onFile(file: File) {
    if (busy) return
    if (!sendRef.current) {
      toast.error('Chat is not connected yet. Try again in a moment.')
      return
    }
    try {
      const uploaded = await uploadChatFile(file, agent.id)
      const extracted = uploaded.extracted_text || `[Uploaded ${uploaded.filename}]`
      setLines((prev) => [
        ...prev,
        { role: 'user', content: extracted, file_name: uploaded.filename, pending: true },
      ])
      sendRef.current({
        content: extracted,
        display_content: uploaded.filename,
        file_name: uploaded.filename,
      })
      setBusy(true)
      setInfo(null)
    } catch (error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Upload failed')
    }
  }

  return (
    <div className="grid h-full min-h-0 overflow-hidden grid-cols-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[220px_minmax(0,1fr)] md:grid-rows-1">
      <aside className="min-h-0 max-h-36 overflow-y-auto p-3 md:max-h-none md:border-r md:border-border">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-xs font-medium text-muted-foreground">Sessions</p>
          <Button size="sm" variant="ghost" aria-label="New session" onClick={() => newSession.mutate()}>
            <Plus className="size-3.5" />
          </Button>
        </div>
        <ul className="space-y-1">
          {sessionList.map((session) => (
            <li key={session.id} className="rounded-lg hover:bg-muted">
              {renamingId === session.id ? (
                <form
                  className="flex gap-1 p-1"
                  onSubmit={(event) => {
                    event.preventDefault()
                    void renameSession(agent.id, session.id, renameTitle).then(() => {
                      setRenamingId(null)
                      void queryClient.invalidateQueries({ queryKey: ['sessions', agent.id] })
                    })
                  }}
                >
                  <input
                    className="h-7 min-w-0 flex-1 rounded border border-input bg-transparent px-1 text-xs"
                    value={renameTitle}
                    onChange={(event) => setRenameTitle(event.target.value)}
                  />
                </form>
              ) : (
                <div className="flex items-center">
                  <button
                    type="button"
                    onClick={() => navigate(`/app/agents/${agent.id}/chat/${session.id}`)}
                    className={cn(
                      'min-w-0 flex-1 truncate px-2 py-1.5 text-left text-xs',
                      session.id === activeId && 'font-medium',
                    )}
                  >
                    {session.title || 'Untitled'}
                  </button>
                  <button
                    type="button"
                    className="px-1 text-[10px] text-muted-foreground"
                    onClick={() => {
                      setRenamingId(session.id)
                      setRenameTitle(session.title)
                    }}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="px-1 text-[10px] text-muted-foreground"
                    onClick={() =>
                      void deleteSession(agent.id, session.id).then(() => {
                        void queryClient.invalidateQueries({ queryKey: ['sessions', agent.id] })
                        if (session.id === activeId) navigate(`/app/agents/${agent.id}/chat`)
                      })
                    }
                  >
                    Del
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex h-full min-h-0 flex-col overflow-hidden">
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
          {visible.map((line, index) => (
            <article
              key={`${line.created_at ?? 'row'}-${index}`}
              className={cn(
                'max-w-2xl rounded-2xl px-3.5 py-2.5 text-sm whitespace-pre-wrap',
                line.role === 'user' ? 'ml-auto bg-primary text-primary-foreground' : 'bg-muted',
                line.role === 'tool' && 'bg-card text-muted-foreground',
              )}
            >
              {line.role === 'assistant' ? <ChatMarkdown text={line.content} /> : line.content}
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
          {busy && !live ? <ThinkingStatus /> : null}
          {info ? <p className="text-xs text-muted-foreground">{info}</p> : null}
        </div>
        <form
          className="shrink-0 border-t border-border bg-background p-3"
          onSubmit={(event) => {
            event.preventDefault()
            void send()
          }}
        >
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
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
              disabled={busy}
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void onFile(file)
                event.target.value = ''
              }}
            />
            <Button type="submit" disabled={busy || !draft.trim()}>
              {busy ? <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> : <Send className="size-4" />}
              Send
            </Button>
          </div>
        </form>
      </section>
    </div>
  )
}
