import { z } from 'zod'

import type { JsonObject, JsonValue, ToolUpdateInput } from '../lib/tools-api.ts'
import { jsonObjectText } from './tools-forms.ts'

const secretKeys = new Set(['api_key', 'private_key', 'auth_code', 'password', 'secret'])
type ConfigPath = readonly (string | number)[]
type ConfigContext = { readonly config: JsonObject; readonly config_schema: JsonObject }
const isJsonArray = (value: JsonValue): value is readonly JsonValue[] => Array.isArray(value)

function sensitiveKeys(schema: JsonObject): ReadonlySet<string> {
  const keys = new Set(secretKeys)
  const fields = schema.fields
  if (fields && isJsonArray(fields)) {
    for (const field of fields) {
      if (field && typeof field === 'object' && !isJsonArray(field) && field.type === 'password' && typeof field.key === 'string') {
        keys.add(field.key)
      }
    }
  }
  return keys
}
const isMasked = (value: JsonValue) => typeof value === 'string' && value.startsWith('****')
const isPopulated = (value: JsonValue) => value !== null && !(typeof value === 'string' && !value.trim())

function inspectConfig(value: JsonValue, keys: ReadonlySet<string>, path: ConfigPath = []): {
  readonly clean: JsonValue; readonly required: readonly ConfigPath[]
} {
  if (isJsonArray(value)) {
    const children = value.map((entry, index) => inspectConfig(entry, keys, [...path, index]))
    return { clean: children.map((child) => child.clean), required: children.flatMap((child) => child.required) }
  }
  if (value === null || typeof value !== 'object') return isMasked(value)
    ? { clean: null, required: [path] } : { clean: value, required: [] }
  const clean: Record<string, JsonValue> = {}
  const required: ConfigPath[] = []
  for (const [key, entry] of Object.entries(value)) {
    const entryPath = [...path, key]
    if (keys.has(key) || isMasked(entry)) {
      if (isPopulated(entry)) required.push(entryPath)
    } else {
      const child = inspectConfig(entry, keys, entryPath)
      clean[key] = child.clean
      required.push(...child.required)
    }
  }
  return { clean, required }
}

export function configurationDraft(tool: ConfigContext) {
  const inspected = inspectConfig(tool.config, sensitiveKeys(tool.config_schema))
  return { text: JSON.stringify(inspected.clean, null, 2), required: inspected.required }
}
function valueAt(config: JsonValue, path: ConfigPath): JsonValue | undefined {
  let value: JsonValue | undefined = config
  for (const part of path) {
    if (value === null || typeof value !== 'object') return undefined
    value = isJsonArray(value) ? (typeof part === 'number' ? value[part] : undefined) : value[String(part)]
  }
  return value
}
function containsMask(value: JsonValue): boolean {
  if (isMasked(value)) return true
  if (isJsonArray(value)) return value.some(containsMask)
  return value !== null && typeof value === 'object' && Object.values(value).some(containsMask)
}
export function configurationSchema(required: readonly ConfigPath[]) {
  return z.object({
    config: jsonObjectText.superRefine((config, ctx) => {
      if (containsMask(config)) ctx.addIssue({ code: 'custom', message: 'Masked values cannot be saved. Re-enter the actual secrets.' })
      if (required.some((path) => {
        const value = valueAt(config, path)
        return typeof value !== 'string' || !value.trim() || isMasked(value)
      })) ctx.addIssue({ code: 'custom', message: 'Re-enter every existing secret listed above. Secret removal is not supported here.' })
    }),
    confirmed: z.boolean().refine(Boolean, 'Confirm that this replaces the entire configuration.'),
  })
}
export function configurationInput(config: JsonObject, tenantId: string): ToolUpdateInput {
  return { config, tenant_id: tenantId }
}
export function configPathLabel(path: ConfigPath): string {
  return '/' + path.map((part) => String(part).replaceAll('~', '~0').replaceAll('/', '~1')).join('/')
}
