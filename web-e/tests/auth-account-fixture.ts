import type { TestContext } from 'node:test'
import { fetchCurrentUser } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { createAuthSessionController } from '../src/lib/auth-session-controller.ts'
import type { TokenResponse } from '../src/lib/types/auth.ts'

export function ownAccountSession(context: TestContext, session: TokenResponse) {
  const values = new Map<string, string>()
  const localStorage: Storage = {
    get length() { return values.size },
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: (key) => { values.delete(key) },
    clear: () => values.clear(),
    key: (index) => [...values.keys()][index] ?? null,
  }
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage } })
  authSession.applySession(authSession.capture(), session)
  const controller = createAuthSessionController(authSession, (token, owner, signal) => fetchCurrentUser(token, signal, owner))
  context.after(() => {
    controller.cancelRefresh()
    authSession.logout(authSession.capture())
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  return { controller, owner: authSession.capture() }
}
