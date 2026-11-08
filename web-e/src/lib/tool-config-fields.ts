import { z } from 'zod'

const privileged = new Set(['allow_network', 'http_proxy', 'https_proxy', 'no_proxy'])
const fieldSchema = z.object({
  key: z.string().min(1), label: z.string().optional(), type: z.string().default('text'),
  placeholder: z.string().optional(),
  options: z.array(z.object({ value: z.string(), label: z.string() })).optional(),
}).transform((field) => ({ ...field, label: field.label ?? field.key })).readonly()
export type ConfigField = z.infer<typeof fieldSchema>
export type FieldEdit = { readonly operation: 'preserve' | 'set' | 'unset'; readonly value: string }
export type FieldEdits = Readonly<Record<string, FieldEdit>>
export const preservedField: FieldEdit = { operation: 'preserve', value: '' }

export function memberConfigFields(schema: unknown): readonly ConfigField[] {
  return z.object({ fields: z.array(fieldSchema).default([]) }).parse(schema).fields.filter((field) => !privileged.has(field.key))
}

export function memberFieldOperations(fields: readonly ConfigField[], edits: FieldEdits) {
  const set: Record<string, string | number | boolean> = {}
  const unset: string[] = []
  for (const field of fields) {
    if (privileged.has(field.key)) continue
    const edit = edits[field.key] ?? preservedField
    switch (edit.operation) {
      case 'preserve': break
      case 'unset': unset.push(field.key); break
      case 'set': {
        const value = edit.value
        if (!value.trim() || value.startsWith('****')) throw new Error(`Enter a new value for ${field.label}.`)
        switch (field.type) {
          case 'number': set[field.key] = z.coerce.number().finite().parse(value); break
          case 'boolean': set[field.key] = z.enum(['true', 'false']).parse(value) === 'true'; break
          default: set[field.key] = value
        }
        break
      }
    }
  }
  return { set, unset }
}
