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
  listLlmModels,
  listSessionMessages,
  listSessions,
  renameSession,
  uploadChatFile,
  type AgentOut,
  type ChatMessage,
} from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

type Line = ChatMessage & { pending?: boolean }

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
  const [info, setInfo] = useState<string | null>(null)
  const [livePreview, setLivePreview] = useState<{ env?: string; screenshot?: string } | null>(null)
  const [modelId, setModelId] = useState(agent.primary_model_id ?? '')
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const sendRef = useRef<((payload: ChatOutbound) => void) | null>(null)
  const liveRef = useRef('')
  const bottomRef = useRef<HTMLDivElement | null>(null)

  const sessionsQuery = useQuery({
    queryKey: ['sessions', agent.id],
    queryFn: () => listSessions(agent.id, user?.id === agent.creator_id ? 'all' : 'mine'),
  })
  const models = useQuery({ queryKey: ['llm-models'], queryFn: listLlmModels })

  const activeId = sessionId ?? sessionsQuery.data?.[0]?.id

  const historyQuery = useQuery({
    queryKey: ['messages', agent.id, activeId],
    queryFn: () => listSessionMessages(agent.id, activeId!),
    enabled: Boolean(activeId),
  })

  useEffect(() => {
    if (historyQuery.data) setLines(historyQuery.data)
  }, [historyQuery.data])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [lines, live])

  useEffect(() => {
    if (!token || !activeId) return
    let greeted = false
    const conn = connectAgentChat(agent.id, token, activeId, {
      onEvent(event: ChatInbound) {
        if (event.type === 'connected') {
          if (agent.onboarded_for_me === false && !greeted) {
            greeted = true
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
        if (event.type === 'info') {
          setInfo(event.content ?? null)
          return
        }
        if (event.type === 'error') {
          setInfo(event.content ?? 'Chat error')
          setBusy(false)
          return
        }
        if (event.type === 'done') {
          const content = event.content || liveRef.current
          if (content) setLines((prev) => [...prev, { role: event.role ?? 'assistant', content }])
          liveRef.current = ''
          setLive('')
          setThinking('')
          setBusy(false)
          return
        }
        if (event.type === 'onboarded') {
          void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
        }
      },
      onClose(code) {
        if (code === 4001) toast.error('Session expired. Sign in again.')
        if (code === 4003) toast.error('This agent is expired or you cannot use it.')
        setBusy(false)
      },
    })
    sendRef.current = conn.send
    return () => {
      sendRef.current = null
      conn.close()
    }
  }, [agent.id, agent.onboarded_for_me, activeId, token, queryClient])

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
    if (!text || !sendRef.current) return
    setDraft('')
    setLines((prev) => [...prev, { role: 'user', content: text }])
    setBusy(true)
    sendRef.current({ content: text, model_id: modelId || undefined })
  }

  async function onFile(file: File) {
    try {
      const uploaded = await uploadChatFile(file, agent.id)
      const extracted = uploaded.extracted_text || `[Uploaded ${uploaded.filename}]`
      setLines((prev) => [...prev, { role: 'user', content: extracted, file_name: uploaded.filename }])
      sendRef.current?.({
        content: extracted,
        display_content: uploaded.filename,
        file_name: uploaded.filename,
      })
      setBusy(true)
    } catch (error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Upload failed')
    }
  }

  return (
    <div className="grid h-full min-h-[32rem] grid-cols-1 md:grid-cols-[220px_1fr]">
      <aside className="border-b border-border p-3 md:border-r md:border-b-0">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-xs font-medium text-muted-foreground">Sessions</p>
          <Button size="sm" variant="ghost" onClick={() => newSession.mutate()}>
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

      <section className="flex min-h-0 flex-col">
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
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
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
          ) : (
            <div className="sr-only" aria-live="polite">
              {busy ? 'Assistant is responding' : ''}
            </div>
          )}
          {info ? <p className="text-xs text-muted-foreground">{info}</p> : null}
          <div ref={bottomRef} />
        </div>
        <form
          className="border-t border-border p-3"
          onSubmit={(event) => {
            event.preventDefault()
            void send()
          }}
        >
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={`Message ${agent.name}…`}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void send()
              }
            }}
          />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <select
              className="h-9 max-w-48 rounded-lg border border-input bg-transparent px-2 text-xs"
              value={modelId}
              onChange={(event) => setModelId(event.target.value)}
            >
              <option value="">Default model</option>
              {(models.data ?? []).map((model) => (
                <option key={model.id} value={model.id}>
                  {model.provider} / {model.model}
                </option>
              ))}
            </select>
            <input
              type="file"
              className="text-xs"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void onFile(file)
                event.target.value = ''
              }}
            />
            <Button type="submit" disabled={busy || !draft.trim()}>
              {busy ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
              Send
            </Button>
          </div>
        </form>
      </section>
    </div>
  )
}
