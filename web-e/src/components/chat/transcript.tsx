import { CheckCheck } from 'lucide-react'
import { memo } from 'react'
import { Button } from '@/components/ui/button'
import type { ChatLine } from '@/lib/chat/transcript'
import { cn } from '@/lib/utils'
import { ChatMessageContent } from './message-content'

const messageTime = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })

type RowProps = {
  readonly agentId: string
  readonly line: ChatLine
  readonly entry: string
  readonly slow: boolean
  readonly socketStopped: boolean
  readonly onRetry: (line: ChatLine) => void
}

const TranscriptRow = memo(function TranscriptRow({ agentId, line, entry, slow, socketStopped, onRetry }: RowProps) {
  const date = line.created_at ? new Date(line.created_at) : null
  const hasTime = date !== null && Number.isFinite(date.getTime())
  const accepted = line.role === 'user' && !line.pending && (line.delivery === 'accepted'
    || (line.delivery === undefined && line.fromHistory && Boolean(line.message_id)))
  return <article data-chat-entry={entry} className={cn(
    'text-sm whitespace-pre-wrap',
    line.role === 'user' ? 'chat-bubble chat-user-bubble ml-auto mr-2 rounded-tr-none'
      : line.role === 'assistant' ? 'chat-bubble chat-agent-bubble ml-2 rounded-tl-none'
        : 'max-w-2xl rounded-2xl bg-muted px-3.5 py-2.5',
    line.role === 'tool' && 'bg-card text-muted-foreground',
  )}>
    <div data-chat-content><ChatMessageContent agentId={agentId} message={line} /></div>
    {line.delivery === 'rejected' || (line.delivery === 'unknown' && slow) ? <div className="mt-2 space-y-2">
      <p className="text-xs" role="status">{line.delivery === 'rejected' ? line.rejectionNotice : 'Acceptance not confirmed.'}</p>
      {line.retryable !== false ? <Button type="button" size="sm" variant="outline" disabled={socketStopped} onClick={() => onRetry(line)}>
        Retry this message
      </Button> : null}
    </div> : null}
    {(line.role === 'user' || line.role === 'assistant') && (hasTime || accepted) ? <div data-chat-metadata className="chat-message-metadata">
      {hasTime ? <time dateTime={line.created_at ?? undefined}>{messageTime.format(date)}</time> : null}
      {accepted ? <CheckCheck role="img" aria-label="Accepted" className="size-3.5 shrink-0" /> : null}
    </div> : null}
  </article>
})

export const ChatTranscript = memo(function ChatTranscript({ lines, ...props }: Omit<RowProps, 'line' | 'entry'> & { readonly lines: readonly ChatLine[] }) {
  return lines.map((line, index) => <TranscriptRow key={line.entry_id ?? line.client_submission_id ?? `${line.created_at ?? 'row'}-${index}`}
    {...props} line={line} entry={line.entry_id ?? line.client_submission_id ?? line.message_id ?? `local-${index}`} />)
})
