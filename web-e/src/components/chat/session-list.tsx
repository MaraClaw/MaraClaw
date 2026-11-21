import { useTranslation } from '@/hooks/use-translation'
import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { isOwnConversation, isWritableSession } from '@/lib/chat/sessions'
import type { SessionOut } from '@/lib/engine-contracts.generated'
import { userFacingRequestError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'

type SessionListProps = {
  readonly agentId: string
  readonly userId: string | undefined
  readonly sessions: readonly SessionOut[]
  readonly activeId: string | undefined
  readonly hasMore: boolean
  readonly loadingMore: boolean
  readonly error: unknown
  readonly onLoadMore: () => void
}

export function SessionList({ agentId, userId, sessions, activeId, hasMore, loadingMore, error, onLoadMore }: SessionListProps) {
  const { t: translate, locale } = useTranslation()
  const [open, setOpen] = useState(false)
  const listId = useId()
  if (!hasMore && sessions.every((session) => isOwnConversation(session, userId))) return null
  return <nav aria-label={translate("Other conversations")} className="text-sm">
    <Button type="button" variant="ghost" size="sm" className="h-11" aria-expanded={open} aria-controls={listId} onClick={() => setOpen(!open)}>
      {translate("Other conversations")}</Button>
    {open ? <div id={listId} className="mt-2 space-y-2">
      <ul className="max-h-64 space-y-1 overflow-y-auto">
        {sessions.map((session) => {
          const own = isOwnConversation(session, userId)
          return <li key={session.id}>
            <Link
              className="flex min-h-11 items-center justify-between gap-3 rounded-lg px-3.5 hover:bg-foreground/5 focus-visible:outline-2 focus-visible:outline-ring aria-[current=page]:bg-foreground/10 aria-[current=page]:font-medium"
              to={own ? `/app/agents/${agentId}/chat` : `/app/agents/${agentId}/chat/${session.id}`}
              aria-current={session.id === activeId ? 'page' : undefined}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate">{own ? translate("Your conversation") : session.username ?? session.title}</span>
                {session.user_id !== userId && !isWritableSession(session) ? <Badge variant="outline">{translate("Read-only")}</Badge> : null}
              </span>
              {session.last_message_at ? <span className="shrink-0 text-xs text-muted-foreground">{relativeTime(session.last_message_at, undefined, locale)}</span> : null}
            </Link>
          </li>
        })}
      </ul>
      {error ? <RequestErrorToast error={error} message={userFacingRequestError(error, translate("Could not load older conversations."))} /> : null}
      {hasMore ? <Button type="button" variant="outline" size="sm" className="h-11" disabled={loadingMore} onClick={onLoadMore}>
        {loadingMore ? translate("Loading older conversations…") : error ? translate("Retry older conversations") : translate("Load older conversations")}
      </Button> : null}
    </div> : null}
  </nav>
}
