import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  browseSkillRead,
  browseSkills,
  importSkillFromUrl,
  importSkillToAgent,
  installClawhub,
  listSkills,
  searchClawhub,
  type AgentOut,
} from '@/lib/workspace-api'

export function AgentSkillsPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [skillUrl, setSkillUrl] = useState('')
  const [browsePath, setBrowsePath] = useState('')
  const [browseContent, setBrowseContent] = useState('')
  const catalog = useQuery({ queryKey: ['skills'], queryFn: listSkills })
  const browse = useQuery({ queryKey: ['skill-browse', browsePath], queryFn: () => browseSkills(browsePath) })
  const clawhub = useQuery({
    queryKey: ['clawhub', query],
    queryFn: () => searchClawhub(query),
    enabled: query.trim().length > 1,
  })

  const importSkill = useMutation({
    mutationFn: (skillId: string) => importSkillToAgent(agent.id, skillId),
    onSuccess() {
      toast.success('Skill copied into the agent workspace')
      void queryClient.invalidateQueries({ queryKey: ['files', agent.id] })
    },
  })

  return (
    <div className="grid gap-6 p-6 lg:grid-cols-2">
      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">Catalog</h2>
        <ul className="space-y-2">
          {(catalog.data ?? []).map((skill) => (
            <li key={skill.id} className="flex items-center justify-between gap-2 rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{skill.name}</p>
                <p className="text-xs text-muted-foreground">{skill.description}</p>
              </div>
              <Button size="sm" variant="outline" onClick={() => importSkill.mutate(skill.id)}>
                Install
              </Button>
            </li>
          ))}
        </ul>
      </section>
      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">ClawHub</h2>
        <Input placeholder="Search ClawHub" value={query} onChange={(event) => setQuery(event.target.value)} />
        <ul className="space-y-2">
          {(clawhub.data ?? []).map((row) => (
            <li key={row.slug} className="flex items-center justify-between gap-2 rounded-xl border border-border px-3 py-2">
              <div>
                <p className="text-sm font-medium">{row.name ?? row.slug}</p>
                <p className="text-xs text-muted-foreground">{row.description}</p>
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  void installClawhub(row.slug).then(() => toast.success('Installed from ClawHub'))
                }
              >
                Add
              </Button>
            </li>
          ))}
        </ul>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!skillUrl.trim()) return
            void importSkillFromUrl(skillUrl.trim()).then(() => {
              toast.success('Imported from URL')
              setSkillUrl('')
              void queryClient.invalidateQueries({ queryKey: ['skills'] })
            })
          }}
        >
          <Input placeholder="https://…/skill.md" value={skillUrl} onChange={(event) => setSkillUrl(event.target.value)} />
          <Button type="submit" size="sm" variant="outline">
            Import URL
          </Button>
        </form>
      </section>
      <section className="space-y-3 lg:col-span-2">
        <h2 className="font-display text-lg font-semibold">Browse skill files</h2>
        <div className="flex gap-2 text-xs">
          <button type="button" className="text-primary" onClick={() => setBrowsePath('')}>
            Root
          </button>
          {browsePath ? <span className="text-muted-foreground">{browsePath}</span> : null}
        </div>
        <ul className="grid gap-2 sm:grid-cols-2">
          {(browse.data ?? []).map((entry) => (
            <li key={entry.path}>
              <button
                type="button"
                className="w-full truncate rounded-xl border border-border px-3 py-2 text-left text-sm hover:bg-muted"
                onClick={() => {
                  if (entry.is_dir) {
                    setBrowsePath(entry.path)
                    setBrowseContent('')
                    return
                  }
                  void browseSkillRead(entry.path).then((row) => setBrowseContent(row.content))
                }}
              >
                {entry.is_dir ? '▸ ' : ''}
                {entry.name}
              </button>
            </li>
          ))}
        </ul>
        {browseContent ? (
          <pre className="max-h-80 overflow-auto rounded-xl border border-border bg-muted/40 p-3 text-xs">{browseContent}</pre>
        ) : null}
      </section>
    </div>
  )
}
