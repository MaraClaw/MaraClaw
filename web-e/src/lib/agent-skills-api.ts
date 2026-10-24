import { authSession } from './auth-session.ts'
import { ApiError, StaleSessionError } from './http.ts'
import { listFiles } from './workspace-api.ts'

export type InstalledAgentSkill = {
  readonly name: string
  readonly path: string
}

export async function listInstalledAgentSkills(agentId: string, signal?: AbortSignal): Promise<readonly InstalledAgentSkill[]> {
  const owner = authSession.capture()
  const assertCurrent = () => {
    if (!authSession.isCurrent(owner)) throw new StaleSessionError(owner)
    signal?.throwIfAborted()
  }
  assertCurrent()
  const entries = await listFiles(agentId, 'skills').catch((error: unknown) => {
    assertCurrent()
    if (error instanceof ApiError && error.status === 404) return []
    throw error
  })
  assertCurrent()
  const visible = entries.filter((entry) => !entry.name.startsWith('.'))
  const folders = await Promise.all(visible.filter((entry) => entry.is_dir).map(async (folder) => {
    assertCurrent()
    const files = await listFiles(agentId, folder.path)
    assertCurrent()
    const skill = files.find((file) => !file.is_dir && file.name === 'SKILL.md')
      ?? files.find((file) => !file.is_dir && file.name === 'skill.md')
    return skill ? [{ name: folder.name, path: skill.path }] : []
  }))
  assertCurrent()
  const flat = visible.filter((entry) => !entry.is_dir && entry.name.endsWith('.md') && entry.name.length > 3)
    .map((entry) => ({ name: entry.name.slice(0, -3), path: entry.path }))
  // A folder's entry point wins if a flat file has the same mention name.
  const skills = new Map<string, InstalledAgentSkill>()
  for (const skill of [...folders.flat(), ...flat]) {
    if (!skills.has(skill.name)) skills.set(skill.name, skill)
  }
  return [...skills.values()].sort((left, right) => left.name.localeCompare(right.name))
}
