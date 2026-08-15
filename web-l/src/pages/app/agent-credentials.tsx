import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  createCredential,
  deleteCredential,
  getGogcliStatus,
  listCredentials,
  setGogcliKeyring,
  startGogcliAuth,
} from '@/lib/control-api'
import { updateAgent, type AgentOut } from '@/lib/workspace-api'

export function AgentCredentialsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const canManage = agent.access_level === 'manage'
  const credentials = useQuery({
    queryKey: ['credentials', agent.id],
    queryFn: () => listCredentials(agent.id),
    enabled: canManage,
  })
  const gogcli = useQuery({
    queryKey: ['gogcli', agent.id],
    queryFn: () => getGogcliStatus(agent.id),
    enabled: canManage && Boolean(agent.gogcli_enabled),
    retry: false,
  })
  const [platform, setPlatform] = useState('')
  const [label, setLabel] = useState('')
  const [cookies, setCookies] = useState('')
  const [password, setPassword] = useState('')
  const [email, setEmail] = useState('')

  function fail(error: unknown, fallback: string) {
    toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : fallback)
  }

  const add = useMutation({
    mutationFn: () =>
      createCredential(agent.id, {
        platform,
        display_name: label || platform,
        cookies_json: cookies.trim() || undefined,
        credential_type: 'website',
      }),
    onSuccess() {
      setPlatform('')
      setLabel('')
      setCookies('')
      toast.success('Credential stored')
      void queryClient.invalidateQueries({ queryKey: ['credentials', agent.id] })
    },
    onError(error) {
      fail(error, 'Could not store cookies')
    },
  })

  if (!canManage) {
    return <p className="p-6 text-sm text-muted-foreground">Only managers can see the cookie vault and gogcli.</p>
  }

  return (
    <div className="space-y-6 p-6">
      <section className="space-y-3">
        <div>
          <h2 className="font-display text-lg font-semibold">Cookie vault</h2>
          <p className="text-sm text-muted-foreground">
            Encrypted session cookies for AgentBay logins. The JSON is never returned after save.
          </p>
        </div>
        <ul className="space-y-2">
          {(credentials.data ?? []).map((item) => (
            <li key={item.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{item.display_name || item.platform}</p>
                <p className="text-xs text-muted-foreground">
                  {item.platform} · {item.has_cookies ? 'cookies stored' : 'empty'}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant="soft">{item.status}</Badge>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    void deleteCredential(agent.id, item.id).then(() => {
                      toast.success('Removed')
                      void queryClient.invalidateQueries({ queryKey: ['credentials', agent.id] })
                    })
                  }
                >
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <Label htmlFor="plat">Domain</Label>
            <Input id="plat" value={platform} onChange={(event) => setPlatform(event.target.value)} placeholder="example.com" />
          </div>
          <div>
            <Label htmlFor="clabel">Label</Label>
            <Input id="clabel" value={label} onChange={(event) => setLabel(event.target.value)} />
          </div>
        </div>
        <div>
          <Label htmlFor="cookies">cookies_json (array)</Label>
          <Textarea
            id="cookies"
            className="font-mono text-xs"
            value={cookies}
            onChange={(event) => setCookies(event.target.value)}
            placeholder='[{"name":"sid","value":"...","domain":"example.com"}]'
          />
        </div>
        <Button size="sm" disabled={!platform.trim()} onClick={() => add.mutate()}>
          Save credential
        </Button>
      </section>

      <section className="space-y-3 rounded-2xl border border-border p-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="font-display text-lg font-semibold">gogcli</h2>
            <p className="text-sm text-muted-foreground">Google CLI inside the agent container. Needs a running container.</p>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={Boolean(agent.gogcli_enabled)}
              onChange={(event) =>
                void updateAgent(agent.id, { gogcli_enabled: event.target.checked }).then(() => {
                  toast.success(event.target.checked ? 'gogcli enabled' : 'gogcli disabled')
                  void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
                })
              }
            />
            Enabled
          </label>
        </div>
        {agent.gogcli_enabled ? (
          <>
            <p className="text-sm">
              {gogcli.data?.authenticated ? `Signed in as ${gogcli.data.account_hint ?? 'account'}` : gogcli.data?.detail || 'Not signed in'}
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <Label htmlFor="kr">Keyring password</Label>
                <Input id="kr" type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
              </div>
              <div>
                <Label htmlFor="gemail">Google account</Label>
                <Input id="gemail" value={email} onChange={(event) => setEmail(event.target.value)} />
              </div>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!password}
                onClick={() =>
                  void setGogcliKeyring(agent.id, password)
                    .then(() => toast.success('Keyring saved'))
                    .catch((error) => fail(error, 'Keyring failed'))
                }
              >
                Save keyring
              </Button>
              <Button
                size="sm"
                disabled={!email}
                onClick={() =>
                  void startGogcliAuth(agent.id, email)
                    .then((result) => {
                      toast.success(result.detail)
                      window.open(result.auth_url, '_blank', 'noopener')
                    })
                    .catch((error) => fail(error, 'Could not start OAuth'))
                }
              >
                Start Google OAuth
              </Button>
            </div>
          </>
        ) : null}
      </section>
    </div>
  )
}
