import { Download } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { fileTicketUrl, mintFileDownloadTicket } from '@/lib/workspace-api'

export function AttachmentDownload({ agentId, path }: { readonly agentId: string; readonly path: string }) {
  const scope = useWorkspaceSession()
  const active = useRef(false)
  const pending = useRef(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])

  async function download() {
    if (!scope.isCurrent() || pending.current) return
    pending.current = true
    setBusy(true)
    try {
      const { ticket } = await mintFileDownloadTicket(agentId, path, { inline: false })
      if (!active.current || !scope.isCurrent()) return
      const link = document.createElement('a')
      link.href = fileTicketUrl(agentId, path, ticket, { inline: false })
      link.download = ''
      link.rel = 'noreferrer'
      link.click()
    } catch (error) {
      if (active.current && scope.isCurrent()) toast.error(userFacingRequestError(error, 'Could not download this file. Try again.'))
    } finally {
      pending.current = false
      if (active.current && scope.isCurrent()) setBusy(false)
    }
  }

  return <Button type="button" variant="outline" size="sm" className="min-h-11" disabled={busy} onClick={() => void download()}>
    <Download className="size-4" aria-hidden />{busy ? 'Preparing…' : 'Download'}
  </Button>
}
