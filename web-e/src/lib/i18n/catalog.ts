import { common } from './common.ts'
import { landing } from './landing.ts'
import { auth } from './auth.ts'
import { workspace } from './workspace.ts'

export const vietnamese = { ...landing, ...auth, ...workspace, ...common } as const
export type TranslationKey = keyof typeof vietnamese
