import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { listCredentials, type CredentialCapture } from '@/lib/control-api'

export function ControlCaptureFields({ agentId, canManage, capture, onCapture, domain, onDomain, busy }: {
  readonly agentId: string
  readonly canManage: boolean
  readonly capture: CredentialCapture | null
  readonly onCapture: (capture: CredentialCapture | null) => void
  readonly domain: string
  readonly onDomain: (domain: string) => void
  readonly busy: boolean
}) {
  const scope = useWorkspaceSession()
  const credentials = useQuery({
    queryKey: ['credentials', agentId],
    queryFn: ({ signal }) => listCredentials(agentId, { signal, owner: scope.owner }),
    enabled: canManage,
    retry: false,
  })
  const selectedId = capture?.mode === 'replace' ? capture.credential_id : null
  return (
    <div className="space-y-2">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-sm">
          Save cookies on release
          <Select className="mt-1" value={selectedId ?? capture?.mode ?? 'skip'} disabled={busy}
            onChange={scope.effect((event) => {
              const value = event.target.value
              if (value === 'skip') onCapture(null)
              else if (value === 'create') onCapture({ mode: 'create' })
              else {
                const selected = credentials.data?.find((item) => item.id === value)
                if (selected) onCapture({ mode: 'replace', credential_id: selected.id, expected_revision: selected.revision })
              }
            })}
          >
            <option value="create">Create new credential</option>
            <option value="skip">Do not save cookies</option>
            {canManage && credentials.data?.length ? (
              <optgroup label="Replace saved credential">
                {credentials.data.map((item) => (
                  <option key={item.id} value={item.id}>{item.display_name || item.platform} ({item.platform})</option>
                ))}
              </optgroup>
            ) : null}
            {selectedId && !credentials.data?.some((item) => item.id === selectedId) ? (
              <option value={selectedId}>Previously selected credential</option>
            ) : null}
          </Select>
        </label>
        <label className="text-sm">
          Cookie domain on unlock
          <Input className="mt-1" value={domain} disabled={busy || !capture}
            placeholder="example.com" onChange={scope.effect((event) => onDomain(event.target.value))} />
        </label>
      </div>
      <p className="text-sm text-muted-foreground">
        {capture?.mode === 'replace'
          ? `Replace only selected revision ${capture.expected_revision}. Newer changes will not be overwritten.`
          : 'A new credential does not replace any saved login. Saving cookies does not verify website sign-in.'}
      </p>
      {canManage && credentials.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading saved credentials…</p> : null}
      {canManage && credentials.isError ? (
        <p role="alert" className="text-sm text-destructive">Saved credentials could not be loaded. Create a new credential or release without saving.</p>
      ) : null}
    </div>
  )
}
