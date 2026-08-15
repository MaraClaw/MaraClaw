import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  CHANNEL_FIELDS,
  CHANNELS,
  deleteChannel,
  getChannel,
  getChannelWebhook,
  getWechatQr,
  saveChannel,
  type AgentOut,
} from '@/lib/workspace-api'

export function AgentChannelsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()

  return (
    <div className="space-y-4 p-6">
      <p className="text-sm text-muted-foreground">
        Connect this agent to a chat app. Incoming webhooks stay on the engine.
      </p>
      {CHANNELS.map((channel) => (
        <ChannelCard
          key={channel.key}
          agentId={agent.id}
          channel={channel}
          onChanged={() => queryClient.invalidateQueries({ queryKey: ['channel', agent.id, channel.path] })}
        />
      ))}
      <WechatCard agentId={agent.id} />
    </div>
  )
}

function ChannelCard({
  agentId,
  channel,
  onChanged,
}: {
  agentId: string
  channel: (typeof CHANNELS)[number]
  onChanged: () => void
}) {
  const fields = CHANNEL_FIELDS[channel.key] ?? []
  const [values, setValues] = useState<Record<string, string>>({})
  const query = useQuery({
    queryKey: ['channel', agentId, channel.path],
    queryFn: () => getChannel(agentId, channel.path),
  })
  const webhook = useQuery({
    queryKey: ['channel-hook', agentId, channel.path],
    queryFn: () => getChannelWebhook(agentId, channel.path),
    enabled: Boolean(query.data),
  })
  const hookUrl = webhook.data?.webhook_url ?? webhook.data?.url ?? ''

  const save = useMutation({
    mutationFn: () => {
      const body = { ...values }
      if (channel.key === 'feishu') body.channel_type = 'feishu'
      return saveChannel(agentId, channel.path, body)
    },
    onSuccess() {
      toast.success(`${channel.label} saved`)
      onChanged()
    },
    onError() {
      toast.error(`Unable to save ${channel.label}`)
    },
  })

  return (
    <article className="space-y-3 rounded-2xl border border-border p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{channel.label}</h2>
        {query.data ? <span className="text-xs text-muted-foreground">Configured</span> : null}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {fields.map((field) => (
          <label key={field.key} className="space-y-1 text-xs">
            <span className="text-muted-foreground">{field.label}</span>
            <Input
              type={field.secret ? 'password' : 'text'}
              value={values[field.key] ?? ''}
              onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
            />
          </label>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => save.mutate()}>
          Save
        </Button>
        {query.data ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              void deleteChannel(agentId, channel.path).then(() => {
                toast.success('Disconnected')
                onChanged()
              })
            }
          >
            Remove
          </Button>
        ) : null}
      </div>
      {hookUrl ? (
        <div className="space-y-1">
          <Label className="text-xs">Webhook URL</Label>
          <div className="flex gap-2">
            <Input readOnly value={hookUrl} className="font-mono text-xs" />
            <Button
              size="sm"
              variant="outline"
              onClick={() => void navigator.clipboard.writeText(hookUrl).then(() => toast.success('Copied'))}
            >
              Copy
            </Button>
          </div>
        </div>
      ) : null}
    </article>
  )
}

function WechatCard({ agentId }: { agentId: string }) {
  const [image, setImage] = useState<string | null>(null)
  return (
    <article className="space-y-2 rounded-2xl border border-border p-4">
      <h2 className="text-sm font-semibold">WeChat</h2>
      <Button
        size="sm"
        variant="outline"
        onClick={() =>
          void getWechatQr(agentId).then((row) => {
            setImage(row.qrcode_url ?? null)
            toast.success('QR requested')
          })
        }
      >
        Request login QR
      </Button>
      {image ? <img src={image} alt="WeChat login QR" className="max-w-48" /> : null}
    </article>
  )
}
