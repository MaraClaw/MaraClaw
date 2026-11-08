import type { TestContext } from 'node:test'
import { adminSession } from '../src/lib/admin-session.ts'
import { MemoryStorage } from './helpers.ts'

export const rosterTicket = { ticket: 'opaque-roster-ticket', expires_in: 60 }
export const rosterScope = { tenantId: 'alpha', search: ' %_Case+? ' }

export function ownRosterSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: new MemoryStorage() } })
  adminSession.replace('synthetic-roster-token')
  context.after(() => {
    adminSession.logout()
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  return adminSession
}

export function ownDownloadDocument(context: TestContext, fail: boolean) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const events: string[] = []
  const anchor = { href: '', download: '', referrerPolicy: '', click: () => { events.push('click'); if (fail) throw new Error('Download failed') }, remove: () => events.push('remove') }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    createElement: () => anchor, body: { append: () => events.push('append') },
  } })
  context.mock.method(URL, 'createObjectURL', () => { throw new Error('Roster must use native download') })
  context.mock.method(URL, 'revokeObjectURL', (url: string) => events.push(`revoke:${url}`))
  context.after(() => {
    if (previous) Object.defineProperty(globalThis, 'document', previous)
    else Reflect.deleteProperty(globalThis, 'document')
  })
  return { anchor, events }
}
