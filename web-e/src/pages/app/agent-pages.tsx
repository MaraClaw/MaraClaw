import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { userFacingRequestError } from '@/lib/http'
import { showRequestError } from '@/lib/request-error-toast'
import { RequestErrorToast } from '@/components/request-error-toast'
import { listPublishedPages, publishWorkspacePage, unpublishPage } from '@/lib/control-api'
import { listFiles, type AgentOut } from '@/lib/workspace-api'

export function AgentPagesPage() {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const [path, setPath] = useState('workspace/')
  const pages = useQuery({ queryKey: ['pages', agent.id], queryFn: ({ signal }) => listPublishedPages(agent.id, signal) })
  const files = useQuery({
    queryKey: ['files', agent.id, path],
    queryFn: ({ signal }) => listFiles(agent.id, path, signal),
  })

  const publish = useMutation({
    mutationFn: (filePath: string) => publishWorkspacePage(agent.id, filePath),
    onSuccess(page) {
      toast.success(translate("Published {title}", { title: page.title ?? page.source_path }))
      void queryClient.invalidateQueries({ queryKey: ['pages', agent.id] })
    },
    onError(error) {
      toast.error(userFacingRequestError(error, translate("Publish failed")))
    },
  })

  return (
    <div className="grid min-h-[28rem] grid-cols-1 md:grid-cols-[240px_1fr]">
      <aside className="space-y-2 border-b border-border p-3 md:border-r md:border-b-0">
        <p className="text-xs text-muted-foreground">{translate("HTML files in the workspace")}</p>
        {files.isError ? <RequestErrorToast error={files.error} message="Unable to load workspace files. Try again." /> : null}
        <button type="button" className="text-xs text-primary" onClick={scope.effect(() => setPath(''))}>
          {translate("Root")}</button>
        <ul className="space-y-1">
          {(files.data ?? []).map((file) => (
            <li key={file.path}>
              <button
                type="button"
                className="w-full truncate rounded-lg px-2 py-1 text-left text-sm hover:bg-muted"
                onClick={() => {
                  if (!scope.isCurrent()) return
                  if (file.is_dir) {
                    setPath(file.path)
                    return
                  }
                  if (!canManage) return
                  if (!file.name.toLowerCase().endsWith('.html') && !file.name.toLowerCase().endsWith('.htm')) {
                    toast.error(translate("Only .html files can be published"))
                    return
                  }
                  publish.mutate(file.path)
                }}
              >
                {file.is_dir ? `${file.name}/` : file.name}
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="space-y-3 p-4">
        <div>
          <h2 className="font-display text-lg font-semibold">{translate("Published pages")}</h2>
          {pages.isError ? <RequestErrorToast error={pages.error} message="Unable to load published pages. Try again." /> : null}
          <p className="text-sm text-muted-foreground">{translate("Anyone with the link can open `/p/")}{'{id}'}{translate("` without signing in.")}</p>
        </div>
        <ul className="space-y-2">
          {(pages.data ?? []).map((page) => (
            <li key={page.id} className="rounded-xl border border-border px-3 py-2">
              <p className="text-sm font-medium">{page.title || page.source_path}</p>
              <p className="text-xs text-muted-foreground">
                {page.source_path} · {page.view_count} {translate("views")}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                <Button size="sm" variant="outline" asChild>
                  <a href={page.url} target="_blank" rel="noreferrer">
                    {translate("Open")}</a>
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => scope.isCurrent() && void navigator.clipboard.writeText(`${window.location.origin}${page.url}`).then(scope.effect(() => toast.success(translate("Copied")))).catch(scope.rethrowCurrent)}
                >
                  {translate("Copy link")}</Button>
                {canManage ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      scope.isCurrent() && void unpublishPage(page.id).then(() => {
                        if (!scope.isCurrent()) return
                        toast.success(translate("Unpublished"))
                        void queryClient.invalidateQueries({ queryKey: ['pages', agent.id] })
                      }).catch(scope.effect((error: unknown) => showRequestError(userFacingRequestError(error, translate("Unable to unpublish this page. Try again.")), error)))
                    }
                  >
                    {translate("Unpublish")}</Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
        {canManage ? (
          <PublishPath onPublish={(value) => publish.mutate(value)} />
        ) : (
          <p className="text-sm text-muted-foreground">{translate("You can view published links. Publishing needs manage access.")}</p>
        )}
      </div>
    </div>
  )
}

function PublishPath({ onPublish }: { onPublish: (path: string) => void }) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const [value, setValue] = useState('')
  return (
    <div className="flex gap-2">
      <Input
        value={value}
        onChange={scope.effect((event) => setValue(event.target.value))}
        placeholder="workspace/site/index.html"
      />
      <Button size="sm" disabled={!value.trim()} onClick={scope.effect(() => onPublish(value.trim()))}>
        {translate("Publish path")}</Button>
    </div>
  )
}
