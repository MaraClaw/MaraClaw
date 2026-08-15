import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { listNotifications, markAllNotificationsRead, markNotificationRead } from '@/lib/workspace-api'

function memberLink(link?: string | null): string | null {
  if (!link) return null
  if (link.startsWith('/plaza')) return `/app${link}`
  if (link.startsWith('/app/')) return link
  return link
}

export function NotificationsPage() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['notifications'], queryFn: listNotifications })

  const markAll = useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['notifications'] })
    },
  })

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="font-display text-2xl font-semibold">Inbox</h1>
        <Button size="sm" variant="outline" onClick={() => markAll.mutate()}>
          Mark all read
        </Button>
      </div>
      <ul className="space-y-2">
        {(query.data ?? []).map((item) => (
          <li key={item.id} className="rounded-xl border border-border px-3 py-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm font-medium">{item.title}</p>
                {item.body ? <p className="text-sm text-muted-foreground">{item.body}</p> : null}
                {memberLink(item.link) ? (
                  <Link to={memberLink(item.link)!} className="mt-1 inline-block text-xs text-primary">
                    Open
                  </Link>
                ) : null}
              </div>
              {!item.is_read ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    void markNotificationRead(item.id).then(() =>
                      queryClient.invalidateQueries({ queryKey: ['notifications'] }),
                    )
                  }
                >
                  Read
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      {query.data?.length === 0 ? <p className="text-sm text-muted-foreground">No notifications yet.</p> : null}
    </div>
  )
}
