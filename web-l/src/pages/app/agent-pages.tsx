import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError, formatApiDetail } from '@/lib/http'
import { listPublishedPages, publishWorkspacePage, unpublishPage } from '@/lib/control-api'
import { listFiles, type AgentOut } from '@/lib/workspace-api'

export function AgentPagesPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const [path, setPath] = useState('workspace/')
  const pages = useQuery({ queryKey: ['pages', agent.id], queryFn: () => listPublishedPages(agent.id) })
  const files = useQuery({
    queryKey: ['files', agent.id, path],
    queryFn: () => listFiles(agent.id, path),
  })

  const publish = useMutation({
    mutationFn: (filePath: string) => publishWorkspacePage(agent.id, filePath),
    onSuccess(page) {
      toast.success(`Published ${page.title ?? page.source_path}`)
      void queryClient.invalidateQueries({ queryKey: ['pages', agent.id] })
    },
    onError(error) {
      toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : 'Publish failed')
    },
  })

  return (
    <div className="grid min-h-[28rem] grid-cols-1 md:grid-cols-[240px_1fr]">
      <aside className="space-y-2 border-b border-border p-3 md:border-r md:border-b-0">
        <p className="text-xs text-muted-foreground">HTML files in the workspace</p>
        <button type="button" className="text-xs text-primary" onClick={() => setPath('')}>
          Root
        </button>
        <ul className="space-y-1">
          {(files.data ?? []).map((file) => (
            <li key={file.path}>
              <button
                type="button"
                className="w-full truncate rounded-lg px-2 py-1 text-left text-sm hover:bg-muted"
                onClick={() => {
                  if (file.is_dir) {
                    setPath(file.path)
                    return
                  }
                  if (!canManage) return
                  if (!file.name.toLowerCase().endsWith('.html') && !file.name.toLowerCase().endsWith('.htm')) {
                    toast.error('Only .html files can be published')
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
          <h2 className="font-display text-lg font-semibold">Published pages</h2>
          <p className="text-sm text-muted-foreground">Anyone with the link can open `/p/{'{id}'}` without signing in.</p>
        </div>
        <ul className="space-y-2">
          {(pages.data ?? []).map((page) => (
            <li key={page.id} className="rounded-xl border border-border px-3 py-2">
              <p className="text-sm font-medium">{page.title || page.source_path}</p>
              <p className="text-xs text-muted-foreground">
                {page.source_path} · {page.view_count} views
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <Button size="sm" variant="outline" asChild>
                  <a href={page.url} target="_blank" rel="noreferrer">
                    Open
                  </a>
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void navigator.clipboard.writeText(`${window.location.origin}${page.url}`).then(() => toast.success('Copied'))}
                >
                  Copy link
                </Button>
                {canManage ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      void unpublishPage(page.id).then(() => {
                        toast.success('Unpublished')
                        void queryClient.invalidateQueries({ queryKey: ['pages', agent.id] })
                      })
                    }
                  >
                    Unpublish
                  </Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
        {canManage ? (
          <PublishPath onPublish={(value) => publish.mutate(value)} />
        ) : (
          <p className="text-sm text-muted-foreground">You can view published links. Publishing needs manage access.</p>
        )}
      </div>
    </div>
  )
}

function PublishPath({ onPublish }: { onPublish: (path: string) => void }) {
  const [value, setValue] = useState('')
  return (
    <div className="flex gap-2">
      <Input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="workspace/site/index.html"
      />
      <Button size="sm" disabled={!value.trim()} onClick={() => onPublish(value.trim())}>
        Publish path
      </Button>
    </div>
  )
}
