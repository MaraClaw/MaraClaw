import { motion } from 'framer-motion'
import { Building2, LineChart, Settings2, Users } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/hooks/use-auth'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { SearchAnalyticsSnapshot } from '@/pages/search-engine-analytics'

const placeholders = [
  {
    title: 'Companies',
    description: 'Platform company list, create, enable/disable - /api/admin/companies',
    icon: Building2,
    href: '/companies',
    platformAdminOnly: true,
  },
  {
    title: 'Users & roles',
    description: 'Activate members and additional admins for the selected company',
    icon: Users,
    href: '/users',
  },
  {
    title: 'Metrics',
    description: 'Timeseries, leaderboards, retention - /api/admin/metrics/*',
    icon: LineChart,
  },
  {
    title: 'Enterprise settings',
    description: 'LLM pool, SSO/IdP, invitations, tools & skills catalogs',
    icon: Settings2,
  },
] as const

export function OverviewPage() {
  const { user } = useAuth()
  const cards = placeholders.filter(
    (item) => !('platformAdminOnly' in item && item.platformAdminOnly) || isPlatformAdminUser(user),
  )

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-8">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
        className="flex flex-col gap-3"
      >
        <div className="flex flex-wrap items-center gap-2">
          {isPlatformAdminUser(user) ? <Badge variant="soft">Platform</Badge> : <Badge variant="soft">Admin</Badge>}
        </div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">
          Overview
        </h1>
        <p className="max-w-2xl text-muted-foreground">
          {isPlatformAdminUser(user)
            ? 'System search activity is below. Open Search engine for the full dashboard or a single company.'
            : 'You are signed in to the operator console.'}
        </p>
      </motion.div>

      {isPlatformAdminUser(user) ? <SearchAnalyticsSnapshot /> : null}

      <div className="grid gap-4 sm:grid-cols-2">
        {cards.map((item, i) => (
          <motion.div
            key={item.title}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.05 * i, duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
          >
            <Card className="h-full">
              <CardHeader>
                <div className="mb-1 flex size-9 items-center justify-center rounded-xl bg-primary/10 text-primary">
                  <item.icon className="size-4" aria-hidden />
                </div>
                <CardTitle>{item.title}</CardTitle>
                <CardDescription>{item.description}</CardDescription>
              </CardHeader>
              <CardContent>
                {'href' in item && item.href ? (
                  <Link to={item.href} className="text-xs font-medium text-primary hover:underline">
                    Open
                  </Link>
                ) : (
                  <p className="text-xs text-muted-foreground">Coming in following tasks</p>
                )}
              </CardContent>
            </Card>
          </motion.div>
        ))}
      </div>
    </div>
  )
}
