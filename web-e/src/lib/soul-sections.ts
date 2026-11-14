/** Reads and rewrites `## Heading` sections of a soul.md text. Dependency-free so node tests can import it. */

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * A section runs from the line after its heading to the next `## ` heading, or to the end of the text.
 * The heading may carry a subtitle after a dash or colon (`## Work Style - the review flow`).
 */
function locateSection(soul: string, heading: string): { readonly start: number; readonly bodyStart: number; readonly end: number } | null {
  const opening = new RegExp(`^##[ \\t]+${escapeRegExp(heading)}(?:[ \\t]+[-–—:][^\\r\\n]*)?[ \\t]*(?:\\r?\\n|$)`, 'im').exec(soul)
  if (!opening) return null
  const bodyStart = opening.index + opening[0].length
  const next = /^## /m.exec(soul.slice(bodyStart))
  return { start: opening.index, bodyStart, end: next ? bodyStart + next.index : soul.length }
}

/** The trimmed body of `## heading`, or null when the soul has no such section. */
export function soulSection(soul: string, heading: string): string | null {
  const section = locateSection(soul, heading)
  return section ? soul.slice(section.bodyStart, section.end).trim() : null
}

/** Replaces the body of `## heading`, or appends the section when it is absent. */
export function replaceSoulSection(soul: string, heading: string, body: string): string {
  const section = locateSection(soul, heading)
  const text = body.trim()
  if (!section) return `${soul}\n## ${heading}\n${text}\n`
  const before = soul.slice(0, section.bodyStart)
  const rest = soul.slice(section.end)
  return `${before}${before.endsWith('\n') ? '' : '\n'}${text}\n${rest ? `\n${rest}` : ''}`
}

/** Drops `## heading` and its body; the text is unchanged when the section is absent. */
export function removeSoulSection(soul: string, heading: string): string {
  const section = locateSection(soul, heading)
  if (!section) return soul
  const rest = soul.slice(section.end)
  const before = soul.slice(0, section.start)
  return rest ? `${before}${rest}` : before.replace(/\n+$/, '\n')
}
