import { z } from 'zod'
import type {
  apiRequest as exportedApiRequest,
  createHttpClient,
  RequestOptions,
  ResponseSchema,
  SchemaRequestOptions,
} from '../src/lib/http.ts'

// Compile-only proofs checked by `tsc -p tsconfig.tests.json`; this file is never executed.
type Equal<Actual, Expected> =
  (<T>() => T extends Actual ? 1 : 2) extends (<T>() => T extends Expected ? 1 : 2) ? true : false
type Expect<Proof extends true> = Proof
type Not<Proof extends boolean> = Proof extends true ? false : true

type Client = ReturnType<typeof createHttpClient>
type ClientApiRequest = Client['apiRequest']
type Accepts<Args extends readonly unknown[]> = ClientApiRequest extends (...args: Args) => unknown ? true : false

type Parsed = { readonly id: string; readonly total: number }
type Legacy = { readonly legacy: true }

declare const client: Client
declare const exported: typeof exportedApiRequest
declare const form: FormData
declare const methodParser: { parse(input: unknown): Parsed }
declare const propertyParser: { readonly parse: (input: unknown) => Parsed }
declare const stringParser: { readonly parse: (input: string) => Parsed }

const memberSchema = z.object({
  id: z.string().brand<'MemberId'>(),
  name: z.string().default('anonymous'),
  count: z.string().transform((value) => Number(value)),
  tags: z.array(z.string()).readonly(),
})
type MemberOutput = z.output<typeof memberSchema>
type MemberInput = z.input<typeof memberSchema>

const emptySchema = z.undefined().transform(() => 'empty' as const)

// Frozen public types.
export type ResponseSchemaShape = Expect<Equal<
  ResponseSchema<Parsed>,
  { readonly parse: (input: unknown) => Parsed }
>>
export type SchemaRequestOptionsShape = Expect<Equal<
  SchemaRequestOptions<Parsed>,
  RequestOptions & { readonly schema: ResponseSchema<Parsed> }
>>
export type SchemaIsRequiredByOptions = Expect<Not<RequestOptions extends SchemaRequestOptions<Parsed> ? true : false>>
export type ZodSchemaIsResponseSchema = Expect<
  typeof memberSchema extends ResponseSchema<MemberOutput> ? true : false
>

// Structural parsers infer their return type.
const viaMethodParser = client.apiRequest('/member', { schema: methodParser })
const viaPropertyParser = client.apiRequest('/member', { schema: propertyParser })
const viaParserWithOptions = client.apiRequest('/member', { method: 'POST', body: {}, schema: propertyParser })
export type MethodParserInfersOutput = Expect<Equal<typeof viaMethodParser, Promise<Parsed>>>
export type PropertyParserInfersOutput = Expect<Equal<typeof viaPropertyParser, Promise<Parsed>>>
export type ParserWithOptionsInfersOutput = Expect<Equal<typeof viaParserWithOptions, Promise<Parsed>>>

// Zod schemas infer parsed output, not wire input.
const viaZod = client.apiRequest('/member', { schema: memberSchema })
type ZodResult = Awaited<typeof viaZod>
export type ZodInfersOutput = Expect<Equal<ZodResult, MemberOutput>>
export type ZodDoesNotInferInput = Expect<Not<Equal<ZodResult, MemberInput>>>
export type ZodTransformedOutput = Expect<Equal<ZodResult['count'], number>>
export type ZodDefaultedOutput = Expect<Equal<ZodResult['name'], string>>
export type ZodBrandedOutput = Expect<Equal<ZodResult['id'], string & z.$brand<'MemberId'>>>
export type ZodReadonlyOutput = Expect<Equal<ZodResult['tags'], readonly string[]>>
export type ZodTransformInputIsWire = Expect<Equal<MemberInput['count'], string>>
export type ZodDefaultInputIsOptional = Expect<Equal<MemberInput['name'], string | undefined>>

const viaEmptySchema = client.apiRequest('/empty', { method: 'DELETE', schema: emptySchema })
export type TransformFromUndefinedInfersOutput = Expect<Equal<typeof viaEmptySchema, Promise<'empty'>>>

// An explicit type argument may restate the schema output.
const explicitWithSchema = client.apiRequest<Parsed>('/member', { schema: propertyParser })
export type ExplicitTypeArgumentMatchesSchema = Expect<Equal<typeof explicitWithSchema, Promise<Parsed>>>

// Legacy calls keep the unknown default and caller-asserted generics.
const legacyDefault = client.apiRequest('/legacy')
const legacyWithOptions = client.apiRequest('/legacy', { method: 'POST', body: { id: 1 }, token: null })
const legacyAsserted = client.apiRequest<Legacy>('/legacy')
const legacyAssertedWithOptions = client.apiRequest<Legacy>('/legacy', { method: 'PUT' })
const rawUnknown = client.apiRequest<unknown>('/legacy')
export type LegacyDefaultsToUnknown = Expect<Equal<typeof legacyDefault, Promise<unknown>>>
export type LegacyOptionsDefaultToUnknown = Expect<Equal<typeof legacyWithOptions, Promise<unknown>>>
export type LegacyKeepsAssertedType = Expect<Equal<typeof legacyAsserted, Promise<Legacy>>>
export type LegacyOptionsKeepAssertedType = Expect<Equal<typeof legacyAssertedWithOptions, Promise<Legacy>>>
export type RawUnknownStaysUnknown = Expect<Equal<typeof rawUnknown, Promise<unknown>>>
export type SchemaCallIsNotRawUnknown = Expect<Not<Equal<typeof viaPropertyParser, Promise<unknown>>>>

// Schema options must carry a real parser accepting unknown input.
export type AcceptsSchemaOptions = Expect<Accepts<[path: string, options: { readonly schema: typeof propertyParser }]>>
export type AcceptsLegacyOptions = Expect<Accepts<[path: string, options: { readonly method: string }]>>
export type AcceptsNoOptions = Expect<Accepts<[path: string]>>
export type RejectsUndefinedSchema = Expect<Not<Accepts<[path: string, options: { readonly schema: undefined }]>>>
export type RejectsSchemaWithoutParse = Expect<Not<Accepts<[path: string, options: { readonly schema: object }]>>>
export type RejectsParserNarrowerThanUnknown = Expect<Not<Accepts<[
  path: string, options: { readonly schema: typeof stringParser },
]>>>

// The exported singleton shares the factory contract.
export type ExportedSharesFactoryOverloads = Expect<Equal<typeof exportedApiRequest, ClientApiRequest>>
const exportedViaZod = exported('/member', { schema: memberSchema })
const exportedViaParser = exported('/member', { schema: propertyParser })
const exportedLegacy = exported('/legacy')
const exportedAsserted = exported<Legacy>('/legacy', { method: 'PATCH' })
export type ExportedInfersZodOutput = Expect<Equal<typeof exportedViaZod, Promise<MemberOutput>>>
export type ExportedInfersParserOutput = Expect<Equal<typeof exportedViaParser, Promise<Parsed>>>
export type ExportedLegacyDefaultsToUnknown = Expect<Equal<typeof exportedLegacy, Promise<unknown>>>
export type ExportedLegacyKeepsAssertedType = Expect<Equal<typeof exportedAsserted, Promise<Legacy>>>

// Form and binary helpers are unchanged.
const formDefault = client.apiFormRequest('/upload', form)
const formAsserted = client.apiFormRequest<Legacy>('/upload', form)
const binary = client.apiBinaryRequest('/preview')
export type FormDefaultsToUnknown = Expect<Equal<typeof formDefault, Promise<unknown>>>
export type FormKeepsAssertedType = Expect<Equal<typeof formAsserted, Promise<Legacy>>>
export type BinaryReturnsBytes = Expect<Equal<typeof binary, Promise<Uint8Array>>>
