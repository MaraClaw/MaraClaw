import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { useInfiniteQuery, useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { Loader2 } from 'lucide-react'
import { useEffect, useReducer, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { agentFilesQuery, loadedFiles } from '@/lib/agent-files-query'
import { createFilePreview, reduceFilePreview } from '@/lib/file-download'
import { userFacingRequestError } from '@/lib/http'
import { showRequestError } from '@/lib/request-error-toast'
import { formatUploadLimit, getUploadPolicy } from '@/lib/upload-policy-api'
import {
  deleteFileContent,
  fileTicketUrl,
  listFileRevisions,
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
  const { t: translate } = useTranslation()
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
      return <p className="mb-3 text-xs text-muted-foreground" role="status">{translate("Loading preview...")}</p>
    case 'failed':
      return <p className="mb-3 text-xs text-muted-foreground" role="status">{translate("Preview unavailable. Download the file to view it.")}</p>
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
  const { t: translate, locale } = useTranslation()
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { token } = useAuth()
  const queryClient = useQueryClient()
  const [path, setPath] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [version, setVersion] = useState<string | undefined>()
  const uploadPolicy = useQuery({
    queryKey: ['upload-policy'],
    queryFn: ({ signal }) => getUploadPolicy({ signal, owner: scope.owner }),
    retry: false,
  })
  const upload = useMutation({
    mutationFn: (file: File) => uploadWorkspaceFile(agent.id, file, path || 'workspace/knowledge_base'),
    onSuccess() {
      toast.success(translate("Uploaded"))
      void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
    },
    onError(error) { toast.error(userFacingRequestError(error, translate("Upload failed"))) },
  })

  const listing = useInfiniteQuery(agentFilesQuery(scope, agent.id, path))
  const entries = loadedFiles(listing.data?.pages)

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
      toast.error(userFacingRequestError(error, translate("Cannot read file")))
    },
  })

  const save = useMutation({
    mutationFn: () => writeFileContent(agent.id, selected!, draft, version),
    onSuccess() {
      toast.success(translate("Saved"))
      void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
    },
    onError(error) {
      toast.error(userFacingRequestError(error, translate("Save failed")))
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
      toast.error(userFacingRequestError(error, translate("Unable to start download. Try again.")))
    },
  })

  return (
    <div className="grid min-h-[28rem] grid-cols-1 md:grid-cols-[240px_1fr]">
      <aside className="border-b border-border p-3 md:border-r md:border-b-0">
        <div className="mb-2 flex items-center justify-between text-xs">
        <button type="button" className="text-primary" onClick={scope.effect(() => setPath(''))}>
            {translate("Root")}</button>
          <input
            type="file"
            className="w-24 text-[10px]"
            aria-label={translate("Upload workspace file")}
            aria-describedby="workspace-upload-policy"
            disabled={!uploadPolicy.isSuccess || upload.isPending}
            onChange={(event) => {
              if (!scope.isCurrent() || !uploadPolicy.isSuccess || upload.isPending) return
              const file = event.target.files?.[0]
              event.target.value = ''
              if (!file) return
              if (file.size > uploadPolicy.data.workspace_file_max_bytes) {
                toast.error(translate("File exceeds the {limit} workspace upload limit.", { limit: formatUploadLimit(uploadPolicy.data.workspace_file_max_bytes, locale) }))
                return
              }
              upload.mutate(file)
            }}
          />
        </div>
        <div id="workspace-upload-policy" className="mb-3 text-xs text-muted-foreground">
          {uploadPolicy.isError ? <div className="space-y-2">
            <RequestErrorToast error={uploadPolicy.error} message="Could not load upload limits. Uploading is unavailable." />
            <Button type="button" size="sm" variant="outline" disabled={uploadPolicy.isFetching}
              onClick={() => { if (scope.isCurrent()) void uploadPolicy.refetch() }}>{translate("Retry upload limits")}</Button>
          </div> : <p role="status">{uploadPolicy.isSuccess
            ? translate("All files, including images: up to {limit}.", { limit: formatUploadLimit(uploadPolicy.data.workspace_file_max_bytes, locale) })
            : translate("Loading upload limits…")}</p>}
        </div>
        {listing.isPaused || listing.isPending || listing.isFetchingNextPage || (!listing.isError && entries.length === 0) ? <p role="status" className="mb-2 text-xs text-muted-foreground">
          {listing.isPaused ? translate("You are offline. Files will load when you reconnect.")
            : listing.isPending ? translate("Loading files...")
            : listing.isFetchingNextPage ? translate("Loading more files...")
            : !listing.isError && entries.length === 0 ? translate("This folder is empty. Upload a file or return to Root.") : ''}
        </p> : null}
        <ul className="space-y-1">
          {entries.map((entry) => (
            <li key={`${entry.is_dir}:${entry.path}`}>
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
        {listing.isError ? <RequestErrorToast error={listing.error} message={listing.isFetchNextPageError ? 'Could not load more files. Your loaded files are still available.' : 'Could not load files. Try again.'} /> : null}
        {listing.hasNextPage || listing.isError ? (
          <Button type="button" size="sm" variant="outline" className="mt-3 min-h-11 w-full"
            disabled={listing.isFetching || listing.isPaused}
            onClick={() => {
              if (listing.isFetching || !scope.isCurrent()) return
              if (listing.isFetchNextPageError || !listing.isError) void listing.fetchNextPage()
              else void listing.refetch()
            }}>
            {listing.isError ? translate("Retry loading files") : translate("Load more files")}
          </Button>
        ) : null}
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
                      {translate("Download")}</Button>
                    <Button size="sm" variant="outline" onClick={() => scope.isCurrent() && void lockFile(agent.id, selected).then(scope.effect(() => toast.success(translate("Locked for edit")))).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to lock this file.")), error)))}>
                      {translate("Lock")}</Button>
                    <Button size="sm" variant="outline" onClick={() => scope.isCurrent() && void unlockFile(agent.id, selected).then(scope.effect(() => toast.success(translate("Unlocked")))).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to unlock this file.")), error)))}>
                      {translate("Unlock")}</Button>
                  </>
                ) : null}
                <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
                  {translate("Save")}</Button>
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
                      }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to delete this file.")), error)))
                    }}
                  >
                    {translate("Delete")}</Button>
                ) : null}
              </div>
            </div>
            {token && /\.(png|jpe?g|gif|webp|svg)$/i.test(selected) ? (
              <FileImagePreview key={`${agent.id}:${selected}`} agentId={agent.id} path={selected} />
            ) : null}
            <Textarea className="min-h-[22rem] font-mono text-xs" value={draft} onChange={scope.effect((event) => setDraft(event.target.value))} />
            {revisions.isError ? <RequestErrorToast error={revisions.error} message="Unable to load file revisions. Try again." /> : null}
            {(revisions.data ?? []).length > 0 ? (
              <div className="mt-3 space-y-1">
                <p className="text-xs font-medium">{translate("Revisions")}</p>
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
                            toast.success(translate("Restored"))
                            void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
                            openFile.mutate(selected)
                          }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to restore this revision.")), error)))
                        }
                      >
                        {translate("Restore")}</Button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        ) : (
          <p className="p-6 text-sm text-muted-foreground">{translate("Select a file to read or edit.")}</p>
        )}
      </section>
    </div>
  )
}
