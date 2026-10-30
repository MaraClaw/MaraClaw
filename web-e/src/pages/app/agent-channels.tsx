import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAuth } from '@/hooks/use-auth'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  CHANNEL_FIELDS, CHANNELS, channelDraftBody, deleteChannel, getChannel, getChannelCatalog,
  getChannelWebhook, getWechatQr, googleChatCredentialsConfigured, saveChannel, type ChannelCapability,
} from '@/lib/channels-api'
import type { AgentOut } from '@/lib/agents-api'

export function AgentChannelsPage() {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const creator = user?.id === agent.creator_id
  const catalog = useQuery({
    queryKey: ['channel-catalog'],
    queryFn: ({ signal }) => getChannelCatalog({ owner: scope.owner, signal }),
    retry: false,
  })
  return (
    <div className="space-y-4 p-6">
      <p className="text-sm text-muted-foreground">Channel capabilities come from the engine. They do not verify credentials or remote permissions.</p>
      {!creator ? <p className="text-sm text-muted-foreground">Only the agent creator can view or change channel credentials.</p> : null}
      {catalog.isPending ? <p role="status">Loading channel capabilities…</p> : catalog.isError ? (
        <div className="space-y-2">
          <p role="alert">Unable to load channel capabilities. Retry to view available integrations.</p>
          <Button variant="outline" disabled={catalog.isFetching} onClick={scope.effect(() => void catalog.refetch())}>Retry catalog</Button>
        </div>
      ) : catalog.data.map((channel) => (
        <article key={channel.channel_type} aria-label={channel.display_name} className="space-y-3 rounded-2xl border border-border p-4">
          <h2 className="text-base font-semibold">{channel.display_name}</h2>
          <p className="text-sm text-muted-foreground">
            Incoming messages: {channel.supports_inbound ? 'supported' : 'not supported'}. Proactive messages: {channel.supports_proactive ? 'supported' : 'not supported'}.
          </p>
          <ChannelSetup key={`${agent.id}:${creator}`} agentId={agent.id} channel={channel} creator={creator} />
        </article>
      ))}
      {catalog.isSuccess && catalog.data.length === 0 ? <p>No channel integrations are available.</p> : null}
    </div>
  )
}

function ChannelSetup({ agentId, channel, creator }: { readonly agentId: string; readonly channel: ChannelCapability; readonly creator: boolean }) {
  if (channel.transport === 'skill_only') return <p className="text-sm text-muted-foreground">This integration works through skills, not channel configuration.</p>
  if (!channel.supports_inbound && !channel.supports_proactive) return <p className="text-sm text-muted-foreground">Channel setup is not supported for this integration.</p>
  if (!creator) return null
  if (channel.channel_type === 'wechat') return <WechatCard agentId={agentId} />
  const provider = CHANNELS.find((item) => item.key === channel.channel_type)
  if (!provider) return <p className="text-sm text-muted-foreground">This integration has no setup form in this client.</p>
  return <ChannelCard agentId={agentId} channel={channel} path={provider.path} />
}

function ChannelCard({ agentId, channel, path }: { readonly agentId: string; readonly channel: ChannelCapability; readonly path: string }) {
  const scope = useWorkspaceSession()
  const fields = CHANNEL_FIELDS[channel.channel_type] ?? []
  const [values, setValues] = useState<Record<string, string>>({})
  const query = useQuery({
    queryKey: ['channel', agentId, path],
    queryFn: ({ signal }) => getChannel(agentId, path, { owner: scope.owner, signal }),
    retry: false,
  })
  const webhook = useQuery({
    queryKey: ['channel-hook', agentId, path],
    queryFn: ({ signal }) => getChannelWebhook(agentId, path, { owner: scope.owner, signal }),
    enabled: channel.transport === 'webhook' && channel.supports_inbound && Boolean(query.data),
  })
  const hookUrl = webhook.data?.webhook_url ?? webhook.data?.url ?? ''
  const refresh = () => scope.client.invalidateQueries({ queryKey: ['channel', agentId, path] })
  const save = useMutation({
    mutationFn: () => saveChannel({ agentId, path }, channelDraftBody(channel.channel_type, values, query.data), { owner: scope.owner, signal: AbortSignal.timeout(30_000) }),
    onSuccess() {
      setValues({})
      toast.success(`${channel.display_name} saved`)
      void refresh()
    },
  })
  const remove = useMutation({
    mutationFn: () => deleteChannel(agentId, path, { owner: scope.owner, signal: AbortSignal.timeout(30_000) }),
    onSuccess() {
      setValues({})
      toast.success('Disconnected')
      void refresh()
    },
  })
  if (query.isPending) return <p role="status">Loading configuration…</p>
  if (query.isError) return <div className="space-y-2"><p role="alert">Unable to load configuration.</p><Button variant="outline" onClick={scope.effect(() => void query.refetch())}>Retry configuration</Button></div>
  return (
    <div className="space-y-3">
      {query.data ? <p className="text-sm text-muted-foreground">Configuration saved; connection not verified.</p> : null}
      {channel.channel_type === 'google_chat' ? <p className="text-sm text-muted-foreground">
        {googleChatCredentialsConfigured(query.data) ? 'Service account credentials configured.' : 'No service account credentials configured. Inbound-only setup is available.'}
        {' '}Leave credentials untouched to keep the stored account. Supply fresh JSON only to add or replace it.
      </p> : null}
      <form className="space-y-3" onSubmit={scope.effect((event) => { event.preventDefault(); save.mutate() })}>
        <div className="grid gap-3 sm:grid-cols-2">
          {fields.map((field) => (
            <label key={field.key} htmlFor={`${agentId}-${path}-${field.key}`} className={`block space-y-1 text-sm ${field.multiline ? 'sm:col-span-2' : ''}`}>
              <span className="text-muted-foreground">{field.label}</span>
              {field.multiline ? <Textarea
                id={`${agentId}-${path}-${field.key}`} value={values[field.key] ?? ''}
                autoComplete="off" spellCheck={false}
                disabled={save.isPending || remove.isPending}
                onChange={scope.effect((event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value })))}
              /> : <Input
                id={`${agentId}-${path}-${field.key}`} type={field.secret ? 'password' : 'text'} autoComplete="off"
                placeholder={field.secret || field.key === 'client_email' ? 'Leave untouched to keep existing' : undefined}
                value={values[field.key] ?? (channel.channel_type === 'google_chat' && field.key === 'project_number' ? query.data?.app_id ?? '' : '')}
                disabled={save.isPending || remove.isPending}
                onChange={scope.effect((event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value })))}
              />}
            </label>
          ))}
        </div>
        {save.isError ? <p role="alert">Unable to save. Check the fields and supply fresh, unmasked credentials when replacing them.</p> : null}
        {remove.isError ? <p role="alert">Unable to disconnect. Try again.</p> : null}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={save.isPending || remove.isPending}>Save</Button>
          {query.data ? <Button type="button" variant="outline" className="text-destructive" disabled={save.isPending || remove.isPending} onClick={() => remove.mutate()}>Remove</Button> : null}
        </div>
      </form>
      {hookUrl ? <div className="space-y-1">
        <label htmlFor={`${agentId}-${path}-webhook`} className="text-sm">Webhook URL</label>
        <div className="flex gap-2">
          <Input id={`${agentId}-${path}-webhook`} readOnly value={hookUrl} className="font-mono text-xs" />
          <Button variant="outline" onClick={scope.effect(() => void navigator.clipboard.writeText(hookUrl).then(scope.effect(() => toast.success('Copied'))).catch(scope.rethrowCurrent))}>Copy</Button>
        </div>
      </div> : null}
      {webhook.isError ? <p role="alert">Unable to load webhook URL.</p> : null}
    </div>
  )
}

function WechatCard({ agentId }: { readonly agentId: string }) {
  const scope = useWorkspaceSession()
  const qr = useMutation({
    mutationFn: () => getWechatQr(agentId, { owner: scope.owner, signal: AbortSignal.timeout(30_000) }),
  })
  return (
    <div className="space-y-2">
      <Button variant="outline" disabled={qr.isPending} onClick={() => qr.mutate()}>Request login QR</Button>
      {qr.isError ? <p role="alert">Unable to request the login QR. Try again.</p> : null}
      {qr.data?.qrcode_url ? <img src={qr.data.qrcode_url} alt="WeChat login QR" width={192} height={192} className="max-w-48" /> : null}
    </div>
  )
}
