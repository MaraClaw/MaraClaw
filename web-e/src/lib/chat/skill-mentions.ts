import type { InstalledAgentSkill } from '../agent-skills-api.ts'
import type { ChatSubmission } from './protocol.ts'

export type SkillMentionRange = {
  readonly start: number
  readonly end: number
  readonly query: string
}

// A mention must start the message or follow whitespace, never an email or URL.
const mentionTokens = /(?<!\S)@([^\s,!?;:()[\]{}"'`<>\\]*)/gu

export type SkillMentionSegment = {
  readonly start: number
  readonly text: string
  readonly skill: InstalledAgentSkill | null
}

export function resolveSkillMentionSegments(text: string, skills: readonly InstalledAgentSkill[]): readonly SkillMentionSegment[] {
  const installed = new Map(skills.map((skill) => [skill.name, skill]))
  const segments: SkillMentionSegment[] = []
  let cursor = 0
  for (const match of text.matchAll(mentionTokens)) {
    const name = match[1] ?? ''
    const skill = installed.get(name) ?? installed.get(name.replace(/\.+$/, ''))
    if (!skill) continue
    if (match.index > cursor) segments.push({ start: cursor, text: text.slice(cursor, match.index), skill: null })
    cursor = match.index + skill.name.length + 1
    segments.push({ start: match.index, text: text.slice(match.index, cursor), skill })
  }
  if (cursor < text.length) segments.push({ start: cursor, text: text.slice(cursor), skill: null })
  return segments
}

export function findSkillMention(text: string, caret: number): SkillMentionRange | null {
  for (const match of text.matchAll(mentionTokens)) {
    const start = match.index
    const end = start + match[0].replace(/\.+$/, '').length
    if (caret > start && caret <= end) {
      return { start, end, query: text.slice(start + 1, caret) }
    }
  }
  return null
}

export function filterSkillMentions(skills: readonly InstalledAgentSkill[], query: string): readonly InstalledAgentSkill[] {
  const search = query.toLocaleLowerCase()
  return skills.filter((skill) => skill.name.toLocaleLowerCase().includes(search))
}

export function insertSkillMention(text: string, range: SkillMentionRange, skill: InstalledAgentSkill) {
  const insertion = `@${skill.name}`
  const suffix = text.slice(range.end)
  return {
    text: text.slice(0, range.start) + insertion + (suffix.startsWith(' ') ? suffix : ` ${suffix}`),
    caret: range.start + insertion.length + 1,
  }
}

export function buildSkillMentionContent(draft: string, skills: readonly InstalledAgentSkill[]): Pick<ChatSubmission, 'content' | 'display_content'> {
  const text = draft.trim()
  const selected = new Map<string, InstalledAgentSkill>()
  for (const { skill } of resolveSkillMentionSegments(text, skills)) {
    if (skill) selected.set(skill.path, skill)
  }
  if (!selected.size) return { content: text }
  const references = [...selected.values()].map((skill) => `- @${skill.name}: ${JSON.stringify(skill.path)}`).join('\n')
  return {
    content: `Use the installed skills requested below. Before working on the user request, read each exact workspace-relative skill file and use its instructions. Do not guess a different skill path from the mention name.\n${references}\n\nUser request:\n${text}`,
    display_content: text,
  }
}
