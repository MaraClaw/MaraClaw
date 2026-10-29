import { useLayoutEffect, useMemo, useSyncExternalStore } from 'react'

import { adminSession } from '../lib/admin-session.ts'
import { ApiError } from '../lib/http.ts'
import type { SessionOwner, SessionPolicy } from '../lib/session-policy.ts'
import { exportCompanyUsers, type CompanyRosterRequest } from '../lib/users-api.ts'

type ExportState = {
  readonly pending: boolean
  readonly message: string | null
  readonly error: string | null
}
const idle: ExportState = { pending: false, message: null, error: null }

export function downloadRosterCsv(csv: string) {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  let anchor: HTMLAnchorElement | undefined
  try {
    anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'company-users.csv'
    document.body.append(anchor)
    anchor.click()
  } finally {
    anchor?.remove()
    URL.revokeObjectURL(url)
  }
}

/** Mutable epoch owns one view visit, not a company/search equality comparison. */
export function createRosterExporter(policy: SessionPolicy, owner: SessionOwner) {
  let epoch = 0
  let identity: string | undefined
  let controller: AbortController | undefined
  let state = idle
  const listeners = new Set<() => void>()
  function publish(next: ExportState) {
    state = next
    for (const listener of listeners) listener()
  }
  function retire() {
    epoch++
    controller?.abort()
    controller = undefined
    publish(idle)
  }
  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    retire,
    visit(tenantId: string | null, search: string) {
      const next = JSON.stringify([tenantId, search])
      if (identity !== next) { identity = next; retire() }
      owner.signal.addEventListener('abort', retire)
      return () => { owner.signal.removeEventListener('abort', retire); retire() }
    },
    async run(input: CompanyRosterRequest, download = downloadRosterCsv) {
      if (controller || !policy.isCurrent(owner)) return
      const requestEpoch = ++epoch
      const request = new AbortController()
      controller = request
      const signal = AbortSignal.any([owner.signal, request.signal])
      const current = () => epoch === requestEpoch && !signal.aborted && policy.isCurrent(owner)
      publish({ pending: true, message: null, error: null })
      try {
        const { csv } = await exportCompanyUsers(input, signal)
        if (!current()) return
        download(csv)
        if (!current()) return
        publish({ pending: false, message: 'Company CSV downloaded. Empty results contain column headers only.', error: null })
      } catch (error) {
        if (!current()) return
        publish({ pending: false, message: null, error: error instanceof ApiError ? error.message : 'Could not export company users. Try again.' })
      } finally {
        if (current()) controller = undefined
      }
    },
  }
}

export function useUsersRosterExport(scope: { readonly tenantId: string | null; readonly search: string }, owner: SessionOwner) {
  const exporter = useMemo(() => createRosterExporter(adminSession, owner), [owner])
  const state = useSyncExternalStore(exporter.subscribe, exporter.getSnapshot, exporter.getSnapshot)
  useLayoutEffect(() => exporter.visit(scope.tenantId, scope.search), [exporter, scope.tenantId, scope.search])
  return {
    ...state,
    retire: exporter.retire,
    start: () => { if (scope.tenantId) void exporter.run({ tenantId: scope.tenantId, search: scope.search }) },
  }
}
