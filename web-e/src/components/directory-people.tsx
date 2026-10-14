import { Loader2 } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { ApiError } from '@/lib/http'

export function PersonCardList({ loading, error, count, empty, errorFallback, children }: {
  readonly loading: boolean
  readonly error: unknown
  readonly count: number
  readonly empty: string
  readonly errorFallback: string
  readonly children: ReactNode
}) {
  if (loading) {
    return (
      <div className="flex h-24 items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
      </div>
    )
  }
  if (error) {
    return (
      <p className="text-sm text-destructive">
        {error instanceof ApiError ? error.message : errorFallback}
      </p>
    )
  }
  if (count === 0) return <p className="text-sm text-muted-foreground">{empty}</p>
  return <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">{children}</div>
}

export function PersonCard({ name, avatarUrl, description, badge, detail }: {
  readonly name: string
  readonly avatarUrl?: string | null
  readonly description?: string | null
  readonly badge: string
  readonly detail?: string | null
}) {
  return (
    <Card className="h-full min-w-0 transition-colors hover:bg-muted/40">
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle className="flex min-w-0 items-center gap-2.5">
            <PersonAvatar name={name} src={avatarUrl} />
            <span className="min-w-0 wrap-break-word">{name}</span>
          </CardTitle>
          {description ? <CardDescription className="wrap-break-word">{description}</CardDescription> : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Badge variant="secondary">{badge}</Badge>
        </div>
      </CardHeader>
      {detail ? (
        <CardContent className="flex flex-wrap items-center gap-3">
          <span className="text-sm text-muted-foreground">{detail}</span>
        </CardContent>
      ) : null}
    </Card>
  )
}

function PersonAvatar({ name, src }: { readonly name: string; readonly src?: string | null }) {
  const scope = useWorkspaceSession()
  const [failed, setFailed] = useState(false)
  if (src && !failed) {
    return (
      <img
        src={src}
        alt=""
        width={28}
        height={28}
        aria-hidden
        draggable={false}
        onError={scope.effect(() => setFailed(true))}
        className="size-7 shrink-0 rounded-full object-cover outline outline-1 -outline-offset-1 outline-black/10 dark:outline-white/10"
      />
    )
  }
  return (
    <span
      aria-hidden
      className="inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-[0.65rem] font-medium text-muted-foreground"
    >
      {initialsFromName(name)}
    </span>
  )
}

function initialsFromName(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) {
    const [first] = parts
    return first.slice(0, 2).toUpperCase()
  }
  const [first, last] = [parts[0], parts[parts.length - 1]]
  return `${first[0] ?? ''}${last[0] ?? ''}`.toUpperCase()
}
