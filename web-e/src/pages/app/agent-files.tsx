import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { Loader2 } from 'lucide-react'
import { useEffect, useReducer, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { createFilePreview, reduceFilePreview } from '@/lib/file-download'
import { ApiError, formatApiDetail, userFacingRequestError } from '@/lib/http'
import {
  deleteFileContent,
  fileTicketUrl,
  listFileRevisions,
  listFiles,
  lockFile,
  mintFileDownloadTicket,
  readFileContent,
  restoreFileRevision,
  unlockFile,
  uploadWorkspaceFile,
  writeFileContent,
  type AgentOut,
} from '@/lib/workspace-api'

function FileImagePreview({ agentId, path }: { agentId: string; path: string }) {
  const scope = useWorkspaceSession()
  const [preview, dispatch] = useReducer(reduceFilePreview, undefined, () => createFilePreview(agentId, path))
  const { status, request } = preview

  useEffect(() => {
    if (status !== 'minting' || !scope.isCurrent()) return
    let active = true
    void mintFileDownloadTicket(request.agentId, request.path, { inline: true }).then(
      ({ ticket }) => {
        if (!active || !scope.isCurrent()) return
        dispatch({ type: 'minted', request, url: fileTicketUrl(request.agentId, request.path, ticket, { inline: true }) })
      },
      () => {
        if (active && scope.isCurrent()) dispatch({ type: 'mint-failed', request })
      },
    )
    return () => { active = false }
  }, [request, scope, status])

  switch (preview.status) {
    case 'minting':
      return <p className="mb-3 text-xs text-muted-foreground" role="status">Loading preview...</p>
    case 'failed':
      return <p className="mb-3 text-xs text-muted-foreground" role="status">Preview unavailable. Download the file to view it.</p>
    case 'ready':
      return (
        <img
          src={preview.url}
          alt={path}
          className="mb-3 max-h-72 rounded-xl border border-border object-contain"
          onError={() => dispatch({ type: 'image-error', request })}
        />
      )
  }
}

export function AgentFilesPage() {
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { token } = useAuth()
  const queryClient = useQueryClient()
  const [path, setPath] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [version, setVersion] = useState<string | undefined>()

  const listing = useQuery({
    queryKey: ['files', agent.id, path],
    queryFn: () => listFiles(agent.id, path),
  })

  const canManage = agent.access_level === 'manage'
  const revisions = useQuery({
    queryKey: ['revisions', agent.id, selected],
    queryFn: () => listFileRevisions(agent.id, selected!),
    enabled: Boolean(selected),
  })

  const openFile = useMutation({
    mutationFn: (filePath: string) => readFileContent(agent.id, filePath),
    onSuccess(data) {
      setSelected(data.path)
      setDraft(data.content)
      setVersion(data.version_token)
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Cannot read file')
    },
  })

  const save = useMutation({
    mutationFn: () => writeFileContent(agent.id, selected!, draft, version),
    onSuccess() {
      toast.success('Saved')
      void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Save failed')
    },
  })

  const download = useMutation({
    mutationFn: async (filePath: string) => {
      const { ticket } = await mintFileDownloadTicket(agent.id, filePath, { inline: false })
      if (!scope.isCurrent()) return
      const link = document.createElement('a')
      link.href = fileTicketUrl(agent.id, filePath, ticket, { inline: false })
      document.body.append(link)
      link.click()
      link.remove()
    },
    onError(error) {
      toast.error(userFacingRequestError(error, 'Unable to start download. Try again.'))
    },
  })

  return (
    <div className="grid min-h-[28rem] grid-cols-1 md:grid-cols-[240px_1fr]">
      <aside className="border-b border-border p-3 md:border-r md:border-b-0">
        <div className="mb-2 flex items-center justify-between text-xs">
        <button type="button" className="text-primary" onClick={scope.effect(() => setPath(''))}>
            Root
          </button>
          <input
            type="file"
            className="w-24 text-[10px]"
            onChange={(event) => {
              if (!scope.isCurrent()) return
              const file = event.target.files?.[0]
              if (!file) return
              void uploadWorkspaceFile(agent.id, file, path || 'workspace/knowledge_base')
                .then(() => {
                  if (!scope.isCurrent()) return
                  toast.success('Uploaded')
                  void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
                })
                .catch((error: unknown) => {
                  if (!scope.isCurrent()) return
                  toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? 'Upload failed') : 'Upload failed')
                })
              event.target.value = ''
            }}
          />
        </div>
        {listing.isLoading ? <Loader2 className="size-4 animate-spin" /> : null}
        <ul className="space-y-1">
          {(listing.data ?? []).map((entry) => (
            <li key={entry.path}>
              <button
                type="button"
                className="w-full truncate rounded-lg px-2 py-1 text-left text-xs hover:bg-muted"
                onClick={() => {
                  if (!scope.isCurrent()) return
                  if (entry.is_dir) setPath(entry.path)
                  else openFile.mutate(entry.path)
                }}
              >
                {entry.is_dir ? '▸ ' : ''}
                {entry.name}
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="flex flex-col p-3">
        {selected ? (
          <>
            <div className="mb-2 flex items-center justify-between gap-2 text-xs">
              <p className="truncate font-medium">{selected}</p>
              <div className="flex gap-2">
                {token ? (
                  <>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={download.isPending}
                      aria-busy={download.isPending}
                      onClick={() => download.mutate(selected)}
                    >
                      {download.isPending ? <Loader2 aria-hidden="true" className="size-3 animate-spin motion-reduce:animate-none" /> : null}
                      Download
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => scope.isCurrent() && void lockFile(agent.id, selected).then(scope.effect(() => toast.success('Locked for edit'))).catch(scope.rethrowCurrent)}>
                      Lock
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => scope.isCurrent() && void unlockFile(agent.id, selected).then(scope.effect(() => toast.success('Unlocked'))).catch(scope.rethrowCurrent)}>
                      Unlock
                    </Button>
                  </>
                ) : null}
                <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
                  Save
                </Button>
                {canManage ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      if (!scope.isCurrent()) return
                      void deleteFileContent(agent.id, selected).then(() => {
                        if (!scope.isCurrent()) return
                        setSelected(null)
                        void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
                      }).catch(scope.rethrowCurrent)
                    }}
                  >
                    Delete
                  </Button>
                ) : null}
              </div>
            </div>
            {token && /\.(png|jpe?g|gif|webp|svg)$/i.test(selected) ? (
              <FileImagePreview key={`${agent.id}:${selected}`} agentId={agent.id} path={selected} />
            ) : null}
            <Textarea className="min-h-[22rem] font-mono text-xs" value={draft} onChange={scope.effect((event) => setDraft(event.target.value))} />
            {(revisions.data ?? []).length > 0 ? (
              <div className="mt-3 space-y-1">
                <p className="text-xs font-medium">Revisions</p>
                <ul className="space-y-1">
                  {(revisions.data ?? []).slice(0, 8).map((rev) => (
                    <li key={rev.id} className="flex items-center justify-between text-xs">
                      <span>
                        {rev.operation} · {rev.created_at?.slice(0, 19) ?? rev.id}
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          scope.isCurrent() && void restoreFileRevision(agent.id, rev.id).then(() => {
                            if (!scope.isCurrent()) return
                            toast.success('Restored')
                            openFile.mutate(selected)
                          }).catch(scope.rethrowCurrent)
                        }
                      >
                        Restore
                      </Button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        ) : (
          <p className="p-6 text-sm text-muted-foreground">Select a file to read or edit.</p>
        )}
      </section>
    </div>
  )
}
