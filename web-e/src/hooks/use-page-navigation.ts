import { useEffect, useRef, useState } from 'react'
import { useQuery, useWorkspaceSession } from './session-workspace'

export function usePageNavigation(queryKey: readonly unknown[]) {
  const scope = useWorkspaceSession()
  const active = useRef(true)
  const revisionKey = ['page-navigation', ...queryKey]
  const revision = useQuery({
    queryKey: revisionKey, queryFn: () => 0, initialData: 0, enabled: false, gcTime: Infinity,
  }).data ?? 0
  const [position, setPosition] = useState<{ readonly cursors: readonly (string | null)[]; readonly revision: number }>({ cursors: [null], revision: 0 })
  const cursors = position.revision === revision ? position.cursors : [null]
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const isCurrent = () => active.current && scope.isCurrent()
  return {
    cursor: cursors.at(-1) ?? null,
    page: cursors.length,
    revision,
    isCurrent,
    previous() {
      if (isCurrent()) setPosition({ revision, cursors: cursors.slice(0, -1) })
    },
    next(cursor: string) {
      if (isCurrent()) setPosition({ revision, cursors: [...cursors, cursor] })
    },
    async reset() {
      if (!scope.isCurrent()) return
      await scope.client.cancelQueries({ queryKey })
      if (!scope.isCurrent()) return
      scope.client.removeQueries({ queryKey })
      scope.client.setQueryData<number>(revisionKey, (held = 0) => held + 1)
    },
  }
}
