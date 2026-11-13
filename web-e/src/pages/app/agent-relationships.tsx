import { useTranslation } from '@/hooks/use-translation'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { usePageNavigation } from '@/hooks/use-page-navigation'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { addRelationship, listRelationshipPage } from '@/lib/relationships-api'
import {
  deleteRelationship,
  listRelationshipCandidates,
  type AgentOut,
} from '@/lib/workspace-api'

const RELATIONS = ['collaborator', 'direct_leader', 'team_member', 'stakeholder', 'subordinate', 'mentor', 'other']

export function AgentRelationshipsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  return <AgentRelationships key={agent.id} agent={agent} />
}

function AgentRelationships({ agent }: { readonly agent: AgentOut }) {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const canManage = agent.access_level === 'manage'
  const prefix = ['rels', agent.id, 'page']
  const navigation = usePageNavigation(prefix)
  const [memberId, setMemberId] = useState('')
  const [relation, setRelation] = useState('collaborator')
  const rows = useQuery({
    queryKey: [...prefix, navigation.revision, navigation.cursor],
    queryFn: ({ signal }) => listRelationshipPage(agent.id, { cursor: navigation.cursor, signal }),
    retry: false,
  })
  const candidates = useQuery({
    queryKey: ['rel-candidates', agent.id],
    queryFn: () => listRelationshipCandidates(agent.id),
    enabled: canManage,
  })

  const add = useMutation({
    mutationFn: () => addRelationship(agent.id, { member_id: memberId, relation, description: '' }),
    onSuccess() {
      void navigation.reset()
      if (!navigation.isCurrent()) return
      setMemberId('')
      toast.success(translate("Relationship saved"))
    },
    onError() {
      if (navigation.isCurrent()) toast.error(translate("Unable to save relationship"))
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteRelationship(agent.id, id),
    onSuccess() { void navigation.reset() },
    onError() { if (navigation.isCurrent()) toast.error(translate("Unable to remove relationship")) },
  })
  const cursorRejected = rows.error instanceof ApiError && rows.error.status === 400

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-6">
      <h2 className="font-display text-lg font-semibold">{translate("People this agent works with")}</h2>
      {canManage ? (
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(event) => {
            if (!scope.isCurrent()) return
            event.preventDefault()
            if (memberId) add.mutate()
          }}
        >
          <Select
            className="min-w-48 flex-1"
            aria-label={translate("Choose a person")}
            value={memberId}
            onChange={scope.effect((event) => setMemberId(event.target.value))}
          >
            <option value="">{translate("Choose a person")}</option>
            {(candidates.data ?? []).map((row) => (
              <option key={row.id} value={row.id}>
                {row.name || row.email}
              </option>
            ))}
          </Select>
          <Select fit aria-label={translate("Relationship")} value={relation} onChange={scope.effect((event) => setRelation(event.target.value))}>
            {RELATIONS.map((item) => (
              <option key={item} value={item}>
                {item.replaceAll('_', ' ')}
              </option>
            ))}
          </Select>
          <Button type="submit" disabled={!memberId || add.isPending}>{translate("Add")}</Button>
        </form>
      ) : null}
      {canManage && candidates.isError ? (
        <div className="space-y-2">
          <RequestErrorToast error={candidates.error} message={userFacingRequestError(candidates.error, translate("Unable to load people to add."))} />
          <Button size="sm" variant="outline" onClick={() => candidates.refetch()}>{translate("Retry people list")}</Button>
        </div>
      ) : null}
      {rows.isLoading ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading relationships…")}</p> : null}
      {rows.isError ? (
        <div className="space-y-2">
          <RequestErrorToast error={rows.error} message={userFacingRequestError(rows.error, translate("Unable to load relationships."))} />
          <Button size="sm" variant="outline" onClick={() => cursorRejected ? navigation.reset() : rows.refetch()}>
            {cursorRejected ? translate("Restart relationships") : translate("Retry relationships")}
          </Button>
        </div>
      ) : null}
      {rows.data?.items.length === 0 ? <p className="text-sm text-muted-foreground">{translate("No relationships yet.")}</p> : null}
      <ul className="space-y-2" aria-label={translate("Relationships")}>
        {(rows.data?.items ?? []).map((row) => (
          <li key={row.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
            <div>
              <p className="text-sm font-medium">{row.member?.name || row.member_id}</p>
              <p className="text-xs text-muted-foreground">{row.member?.email}</p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="soft">{row.relation_label || row.relation}</Badge>
              {canManage ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(row.id)}
                >
                  {translate("Remove")}</Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      <nav aria-label={translate("Relationship pages")} className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={navigation.page === 1 || rows.isFetching} onClick={navigation.previous}>{translate("Previous")}</Button>
        <span role="status" className="text-sm text-muted-foreground">{translate("Page")}{' '}{navigation.page}</span>
        <Button size="sm" variant="outline" disabled={!rows.data?.next_cursor || rows.isFetching} onClick={() => {
          if (rows.data?.next_cursor) navigation.next(rows.data.next_cursor)
        }}>{translate("Next")}</Button>
      </nav>
    </div>
  )
}
