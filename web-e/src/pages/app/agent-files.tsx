import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  deleteFileContent,
  fileDownloadUrl,
  listFileRevisions,
  listFiles,
  lockFile,
  readFileContent,
  restoreFileRevision,
  unlockFile,
  uploadWorkspaceFile,
  writeFileContent,
  type AgentOut,
} from '@/lib/workspace-api'

export function AgentFilesPage() {
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

  return (
    <div className="grid min-h-[28rem] grid-cols-1 md:grid-cols-[240px_1fr]">
      <aside className="border-b border-border p-3 md:border-r md:border-b-0">
        <div className="mb-2 flex items-center justify-between text-xs">
          <button type="button" className="text-primary" onClick={() => setPath('')}>
            Root
          </button>
          <input
            type="file"
            className="w-24 text-[10px]"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (!file) return
              void uploadWorkspaceFile(agent.id, file, path || 'workspace/knowledge_base')
                .then(() => {
                  toast.success('Uploaded')
                  void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
                })
                .catch((error: unknown) => {
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
                    <Button size="sm" variant="outline" asChild>
                      <a href={fileDownloadUrl(agent.id, selected, token)} target="_blank" rel="noreferrer">
                        Download
                      </a>
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => void lockFile(agent.id, selected).then(() => toast.success('Locked for edit'))}>
                      Lock
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => void unlockFile(agent.id, selected).then(() => toast.success('Unlocked'))}>
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
                      void deleteFileContent(agent.id, selected).then(() => {
                        setSelected(null)
                        void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
                      })
                    }}
                  >
                    Delete
                  </Button>
                ) : null}
              </div>
            </div>
            {token && /\.(png|jpe?g|gif|webp|svg)$/i.test(selected) ? (
              <img
                src={fileDownloadUrl(agent.id, selected, token)}
                alt={selected}
                className="mb-3 max-h-72 rounded-xl border border-border object-contain"
              />
            ) : null}
            <Textarea className="min-h-[22rem] font-mono text-xs" value={draft} onChange={(event) => setDraft(event.target.value)} />
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
                          void restoreFileRevision(agent.id, rev.id).then(() => {
                            toast.success('Restored')
                            openFile.mutate(selected)
                          })
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
