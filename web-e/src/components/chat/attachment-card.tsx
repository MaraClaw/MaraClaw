import * as Dialog from '@radix-ui/react-dialog'
import { Eye, File, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { ChatAttachment } from '@/lib/chat/message'
import { AttachmentDownload } from './attachment-download'
import { AttachmentPreviewBody } from './attachment-preview'

export function AttachmentCard({ agentId, attachment }: {
  readonly agentId: string
  readonly attachment: ChatAttachment
}) {
  return <Dialog.Root>
    <div className="flex min-w-0 flex-wrap items-center gap-3 rounded-2xl border border-border bg-card p-3 text-card-foreground shadow-card whitespace-normal">
      <File className="size-5 shrink-0 text-muted-foreground" aria-hidden />
      <p className="min-w-0 flex-1 basis-36 break-words text-sm font-medium [overflow-wrap:anywhere]">{attachment.name}</p>
      <div className="flex flex-wrap gap-2">
        <Dialog.Trigger asChild><Button type="button" variant="outline" size="sm" className="min-h-11"><Eye className="size-4" aria-hidden />Preview</Button></Dialog.Trigger>
        <AttachmentDownload agentId={agentId} path={attachment.path} />
      </div>
    </div>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
      <Dialog.Content className="fixed inset-4 z-50 mx-auto flex max-h-[calc(100dvh-2rem)] max-w-4xl flex-col overflow-hidden rounded-2xl border border-border bg-card text-card-foreground shadow-elevated sm:inset-y-8">
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border p-4 sm:p-6">
          <div className="min-w-0 space-y-1">
            <Dialog.Title className="break-words font-display text-lg font-semibold [overflow-wrap:anywhere]">{attachment.name}</Dialog.Title>
            <Dialog.Description className="text-xs text-muted-foreground">Attachment preview · up to 20 MiB</Dialog.Description>
          </div>
          <Dialog.Close asChild><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0" aria-label="Close preview"><X className="size-4" aria-hidden /></Button></Dialog.Close>
        </header>
        <div className="min-h-0 flex-1 overflow-auto overscroll-contain p-4 sm:p-6">
          <AttachmentPreviewBody key={`${agentId}:${attachment.path}`} agentId={agentId} path={attachment.path} />
        </div>
        <footer className="flex shrink-0 justify-end border-t border-border p-4">
          <AttachmentDownload agentId={agentId} path={attachment.path} />
        </footer>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
