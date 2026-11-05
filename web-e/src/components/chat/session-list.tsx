import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import type { SessionOut } from '@/lib/engine-contracts.generated'
import { userFacingRequestError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'

type SessionListProps = {
  readonly agentId: string
  readonly sessions: readonly SessionOut[]
  readonly activeId: string | undefined
  readonly hasMore: boolean
  readonly loadingMore: boolean
  readonly error: unknown
  readonly onLoadMore: () => void
}

const isShared = (session: SessionOut) => session.source_channel === 'shared'
const sessionLabel = (session: SessionOut) => isShared(session) ? 'Shared conversation' : session.username ?? session.title

export function SessionList({ agentId, sessions, activeId, hasMore, loadingMore, error, onLoadMore }: SessionListProps) {
  const [open, setOpen] = useState(false)
  const listId = useId()
  if (!hasMore && !sessions.some((session) => !isShared(session))) return null
  return <nav aria-label="Other conversations" className="text-sm">
    <Button type="button" variant="ghost" size="sm" aria-expanded={open} aria-controls={listId} onClick={() => setOpen(!open)}>
      Other conversations
    </Button>
    {open ? <div id={listId} className="mt-2 space-y-2">
      <ul className="max-h-64 space-y-1 overflow-y-auto">
        {sessions.map((session) => <li key={session.id}>
          <Link
            className="flex min-h-11 items-center justify-between gap-3 rounded-lg px-2 hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
            to={isShared(session) ? `/app/agents/${agentId}/chat` : `/app/agents/${agentId}/chat/${session.id}`}
            aria-current={session.id === activeId ? 'page' : undefined}
          >
            <span className="truncate">{sessionLabel(session)}</span>
            {session.last_message_at ? <span className="shrink-0 text-xs text-muted-foreground">{relativeTime(session.last_message_at)}</span> : null}
          </Link>
        </li>)}
      </ul>
      {error ? <p role="alert" className="text-xs text-destructive">{userFacingRequestError(error, 'Could not load older conversations.')}</p> : null}
      {hasMore ? <Button type="button" variant="outline" size="sm" disabled={loadingMore} onClick={onLoadMore}>
        {loadingMore ? 'Loading older conversations…' : error ? 'Retry older conversations' : 'Load older conversations'}
      </Button> : null}
    </div> : null}
  </nav>
}
