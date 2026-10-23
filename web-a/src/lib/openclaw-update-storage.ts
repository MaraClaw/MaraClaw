import { openClawUpdateRequestSchema } from './openclaw-updates-api.ts'
import type { OpenClawUpdateRequest } from './openclaw-updates-api.ts'

type SubmissionStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export function createOpenClawUpdateStorage(adminId: string | undefined, storage: () => SubmissionStorage = () => window.sessionStorage) {
  const key = `maraclaw-admin-openclaw-update:${encodeURIComponent(adminId ?? '')}`
  return {
    read(): OpenClawUpdateRequest | null {
      if (!adminId) return null
      const saved = storage().getItem(key)
      if (saved === null) return null
      const value: unknown = JSON.parse(saved)
      return openClawUpdateRequestSchema.parse(value)
    },
    save(request: OpenClawUpdateRequest): void {
      if (!adminId) throw new Error('Sign in again before sending an update notice.')
      storage().setItem(key, JSON.stringify(request))
    },
    clear(): void {
      if (adminId) storage().removeItem(key)
    },
  }
}
