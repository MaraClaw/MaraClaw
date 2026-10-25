import { z } from 'zod'

import { apiRequest, createApiRequest } from '../src/lib/http.ts'

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
  (<Value>() => Value extends Right ? 1 : 2) ? true : false
type Assert<Value extends true> = Value

const transformedSchema = z.string().transform((value) => value.length)
const defaultedSchema = z.string().default('fallback')
const resourceIdSchema = z.string().brand<'ResourceId'>()
const resourceSchema = z.object({ id: resourceIdSchema, count: z.number().default(0) }).readonly()

export function inferRequests(request: typeof apiRequest) {
  return {
    transformed: request('/transformed', { schema: transformedSchema }),
    defaulted: request('/defaulted', { schema: defaultedSchema }),
    resource: request('/resource', { schema: resourceSchema }),
    legacy: request<{ readonly ok: true }>('/legacy'),
    ignored: request<void>('/ignored', { method: 'DELETE' }),
    unknown: request('/unknown'),
  }
}

type Inferred = ReturnType<typeof inferRequests>
export type RequestInferenceProofs = [
  Assert<Equal<typeof apiRequest, ReturnType<typeof createApiRequest>>>,
  Assert<Equal<Inferred['transformed'], Promise<number>>>,
  Assert<Equal<Inferred['defaulted'], Promise<string>>>,
  Assert<Equal<Inferred['resource'], Promise<Readonly<{ id: z.output<typeof resourceIdSchema>; count: number }>>>>,
  Assert<Equal<Inferred['legacy'], Promise<{ readonly ok: true }>>>,
  Assert<Equal<Inferred['ignored'], Promise<void>>>,
  Assert<Equal<Inferred['unknown'], Promise<unknown>>>,
]
