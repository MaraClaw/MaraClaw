import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { roleLabel, type AdminUser } from '@/lib/users-api'

export function UserCard({
  row,
  canToggle,
  pending,
  onToggle,
  children,
}: {
  readonly row: AdminUser
  readonly canToggle: boolean
  readonly pending: boolean
  readonly onToggle: () => void
  readonly children?: ReactNode
}) {
  return (
    <Card className="relative transition-colors hover:bg-muted/40">
      <Link
        to={`/users/${row.id}`}
        aria-label={`Open ${row.display_name || row.email || 'user'}`}
        className="absolute inset-0 z-10 rounded-[inherit] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        onClick={() => sessionStorage.setItem('web-a:users-scroll', String(window.scrollY))}
      />
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>{row.display_name || row.email || 'User'}</CardTitle>
          <CardDescription>{row.email || row.username}</CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">{roleLabel(row.role)}</Badge>
          {row.is_genesis ? <Badge variant="soft">Genesis</Badge> : null}
          <Badge variant={row.is_active ? 'success' : 'destructive'}>
            {row.is_active ? 'Active' : 'Inactive'}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-muted-foreground">{row.agents_count} agents</span>
        {canToggle ? (
          <Button
            variant="ghost"
            size="sm"
            className="relative z-20"
            disabled={pending}
            onClick={onToggle}
          >
            {row.is_active ? 'Deactivate' : 'Activate'}
          </Button>
        ) : null}
        {children}
      </CardContent>
    </Card>
  )
}
