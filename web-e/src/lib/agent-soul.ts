/** The soul.md the New agent form starts from when a template is chosen. Dependency-free so node tests can import it. */
import { removeSoulSection, replaceSoulSection, soulSection } from './soul-sections.ts'

export type SoulFields = {
  readonly role: string
  readonly personality: string
  readonly workStyle: string
  readonly boundaries: string
}

const sections = [['Personality', 'personality'], ['Work Style', 'workStyle'], ['Boundaries', 'boundaries']] as const

/** The fields a template soul implies: its `**Role**` line and its Personality, Work Style and Boundaries bodies. A placeholder role reads as empty. */
export function soulFields(soul: string): SoulFields {
  const role = /^[ \t]*-[ \t]*\*\*Role\*\*:[ \t]*(.*)$/im.exec(soul)?.[1]?.trim() ?? ''
  return {
    role: /\{[^}]*\}/.test(role) ? '' : role,
    personality: soulSection(soul, 'Personality') ?? '',
    workStyle: soulSection(soul, 'Work Style') ?? '',
    boundaries: soulSection(soul, 'Boundaries') ?? '',
  }
}

/** Writes the section fields into the base soul: each replaces its section and an empty one drops it. */
export function composeSoul(base: string, fields: Omit<SoulFields, 'role'>): string {
  let soul = base
  for (const [heading, key] of sections) {
    const body = fields[key].trim()
    soul = body ? replaceSoulSection(soul, heading, body) : removeSoulSection(soul, heading)
  }
  return soul
}

export type SoulIdentity = { readonly name: string; readonly role: string }

type Mark = { readonly field: keyof SoulIdentity; readonly start: number; readonly end: number }

/** Soul editor text plus the spans where the form wrote the name or role in place of their placeholders. */
export type SoulDraft = { readonly text: string; readonly identity: SoulIdentity; readonly marks: readonly Mark[] }

const placeholders = { name: '{{agent_name}}', role: '{{role_description}}' } as const

const identityLines = [
  [/^(#[ \t]+Soul[ \t]+-[ \t]*).*$/m, 'name'],
  [/^([ \t]*-[ \t]*\*\*Name\*\*:[ \t]*).*$/m, 'name'],
  [/^([ \t]*-[ \t]*\*\*Role\*\*:[ \t]*).*$/m, 'role'],
] as const

function oneLine(text: string): string {
  return text.trim().replace(/\s+/g, ' ')
}

function shown(identity: SoulIdentity): SoulIdentity {
  return { name: oneLine(identity.name) || placeholders.name, role: oneLine(identity.role) || placeholders.role }
}

/** Replaces `[start, end)`: marks before or after it shift with the text, marks it touches are dropped. */
function splice(draft: SoulDraft, start: number, end: number, insert: string): SoulDraft {
  const delta = insert.length - (end - start)
  const marks = draft.marks.flatMap((mark) => mark.end <= start
    ? [mark]
    : mark.start >= end ? [{ ...mark, start: mark.start + delta, end: mark.end + delta }] : [])
  return { ...draft, text: draft.text.slice(0, start) + insert + draft.text.slice(end), marks }
}

/** The `# Soul - …` title and first `**Name**` / `**Role**` lines always follow the fields, even after edits. */
function writeIdentityLines(draft: SoulDraft): SoulDraft {
  let next = draft
  for (const [line, field] of identityLines) {
    const match = line.exec(next.text)
    if (match?.[1] !== undefined) next = splice(next, match.index + match[1].length, match.index + match[0].length, next.identity[field])
  }
  return next
}

/** Writes the name and role over every `{{agent_name}}` / `{{role_description}}` and the identity lines, remembering where. */
export function draftSoul(soul: string, identity: SoulIdentity): SoulDraft {
  const values = shown(identity)
  const marks: Mark[] = []
  let text = ''
  let cursor = 0
  for (const match of soul.matchAll(/\{\{(agent_name|role_description)\}\}/g)) {
    const field = match[1] === 'agent_name' ? 'name' : 'role'
    text += soul.slice(cursor, match.index)
    marks.push({ field, start: text.length, end: text.length + values[field].length })
    text += values[field]
    cursor = match.index + match[0].length
  }
  return writeIdentityLines({ text: text + soul.slice(cursor), identity: values, marks })
}

/** Rewrites every remembered name and role span plus the identity lines with new field values. */
export function renameSoul(draft: SoulDraft, identity: SoulIdentity): SoulDraft {
  const values = shown(identity)
  const marks: Mark[] = []
  let text = ''
  let cursor = 0
  for (const mark of draft.marks) {
    text += draft.text.slice(cursor, mark.start)
    marks.push({ field: mark.field, start: text.length, end: text.length + values[mark.field].length })
    text += values[mark.field]
    cursor = mark.end
  }
  return writeIdentityLines({ text: text + draft.text.slice(cursor), identity: values, marks })
}

/** Applies an edit typed into the soul: spans outside the changed range keep following the fields, touched ones stop. */
export function editSoul(draft: SoulDraft, text: string): SoulDraft {
  const before = draft.text
  const limit = Math.min(before.length, text.length)
  let prefix = 0
  while (prefix < limit && before[prefix] === text[prefix]) prefix++
  let suffix = 0
  while (suffix < limit - prefix && before[before.length - 1 - suffix] === text[text.length - 1 - suffix]) suffix++
  return splice(draft, prefix, before.length - suffix, text.slice(prefix, text.length - suffix))
}

/** Fills `{{creator_name}}` (when known) and `{{created_at}}` with the local `YYYY-MM-DD` date, the format the engine writes. */
export function fillCreationDetails(soul: string, creatorName: string, now: Date): string {
  const date = [now.getFullYear(), now.getMonth() + 1, now.getDate()].map((part) => String(part).padStart(2, '0')).join('-')
  const creator = creatorName.trim()
  const dated = soul.replaceAll('{{created_at}}', date)
  return creator ? dated.replaceAll('{{creator_name}}', () => creator) : dated
}
