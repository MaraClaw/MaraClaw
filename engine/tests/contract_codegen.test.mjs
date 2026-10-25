import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, stripTypeScriptTypes } from "node:module";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { generate, main } from "../scripts/contract_codegen/generate.mjs";

const require = createRequire(new URL("../../web-e/package.json", import.meta.url));
const zodModule = process.env.CONTRACT_ZOD_MODULE ?? require.resolve("zod");

function fixture(schemas, roots = Object.keys(schemas)) {
  return { components: { schemas }, "x-maraclaw-contract": { version: 1, roots, schemas: Object.keys(schemas) } };
}

async function runtime(document) {
  const javascript = stripTypeScriptTypes(generate(document)).replace('"zod"', JSON.stringify(pathToFileURL(zodModule).href));
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

test("required nullable differs from optional nullable and rejects extra forbidden fields", async () => {
  // Given
  const document = fixture({ Item: { type: "object", additionalProperties: false, required: ["required"], properties: {
    required: { anyOf: [{ type: "string" }, { type: "null" }] },
    optional: { type: ["string", "null"], default: null },
  } } });
  // When
  const { ItemSchema } = await runtime(document);
  // Then
  assert.equal(ItemSchema.safeParse({ required: null }).success, true);
  assert.equal(ItemSchema.safeParse({}).success, false);
  assert.equal(ItemSchema.safeParse({ required: "x", optional: null }).success, true);
  assert.equal(ItemSchema.safeParse({ required: "x", extra: 1 }).success, false);
});

test("referenced enums reject unknown values and export concrete tuples", async () => {
  // Given
  const document = fixture({ Kind: { type: "string", enum: ["first", "second"] },
    Item: { type: "array", items: { $ref: "#/components/schemas/Kind" }, minItems: 1 } }, ["Item"]);
  // When
  const { ItemSchema, KindValues } = await runtime(document);
  // Then
  assert.deepEqual(KindValues, ["first", "second"]);
  assert.equal(ItemSchema.safeParse(["first"]).success, true);
  assert.equal(ItemSchema.safeParse(["other"]).success, false);
  assert.equal(ItemSchema.safeParse([]).success, false);
});

test("named enum objects use positional metadata instead of deriving member names from values", async () => {
  // Given names deliberately different from any value-derived vocabulary.
  const document = fixture({ Kind: { type: "string", enum: ["second", "first", "prototype"],
    "x-enum-varnames": ["ALPHA", "OMEGA", "__proto__"] } });
  // When
  const { Kind, KindValues, KindSchema } = await runtime(document);
  // Then the named object and existing tuple/schema agree without prototype-special property syntax.
  assert.deepEqual(Kind, { ALPHA: "second", OMEGA: "first", ["__proto__"]: "prototype" });
  assert.deepEqual(KindValues, ["second", "first", "prototype"]);
  assert.deepEqual(Object.values(Kind).map(value => KindSchema.parse(value)), KindValues);
  assert.equal(KindSchema.safeParse("unknown").success, false);
});

for (const metadata of [null, "NAME", [1], [], ["ONE"], ["ONE", "ONE"], ["bad-name", "TWO"], ["1NAME", "TWO"], ["", "TWO"]]) {
  test(`invalid enum member metadata is rejected: ${JSON.stringify(metadata)}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Kind: { enum: ["one", "two"], "x-enum-varnames": metadata } })),
      /array of strings|member count|duplicate enum member|invalid enum member/);
  });
}

for (const schema of [{ type: "string" }, { enum: [] }, { enum: [1] }]) {
  test(`enum metadata without a concrete string vocabulary is rejected: ${JSON.stringify(schema)}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Kind: { ...schema, "x-enum-varnames": ["ONE"] } })), /nonempty string enum/);
  });
}

for (const conflicting of ["KindSchema", "KindValues", "z"]) {
  test(`named enum export collisions are rejected: ${conflicting}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Kind: { enum: ["one"], "x-enum-varnames": ["ONE"] },
      [conflicting]: { type: "string" } })), /export collision/);
  });
}

test("object additionalProperties preserves unconstrained extras and validates typed extras", async () => {
  // Given
  const document = fixture({ Loose: { type: "object", properties: { name: { type: "string" } } },
    Dictionary: { type: "object", additionalProperties: { type: "integer", minimum: 1 } } });
  // When
  const { LooseSchema, DictionarySchema } = await runtime(document);
  // Then
  assert.deepEqual(LooseSchema.parse({ name: "x", extra: 2 }), { name: "x", extra: 2 });
  assert.equal(DictionarySchema.safeParse({ a: 1 }).success, true);
  assert.equal(DictionarySchema.safeParse({ a: 0 }).success, false);
  assert.equal(DictionarySchema.safeParse({ a: 1.5 }).success, false);
});

test("oneOf enforces exclusivity rather than accepting an overlapping union", async () => {
  // Given
  const document = fixture({ Choice: { oneOf: [{ type: "integer" }, { type: "number" }] } });
  // When
  const { ChoiceSchema } = await runtime(document);
  // Then
  assert.equal(ChoiceSchema.safeParse(1.5).success, true);
  assert.equal(ChoiceSchema.safeParse(1).success, false);
});

test("required properties absent from properties obey additionalProperties", async () => {
  // Given
  const document = fixture({ Forbidden: { type: "object", required: ["x"], additionalProperties: false },
    Typed: { type: "object", required: ["x"], additionalProperties: { type: "integer" } } });
  // When
  const { ForbiddenSchema, TypedSchema } = await runtime(document);
  // Then
  assert.equal(ForbiddenSchema.safeParse({ x: 1 }).success, false);
  assert.equal(ForbiddenSchema.safeParse({}).success, false);
  assert.equal(TypedSchema.safeParse({ x: 1 }).success, true);
  assert.equal(TypedSchema.safeParse({ x: "wrong" }).success, false);
});

test("string length bounds count Unicode code points rather than UTF-16 units", async () => {
  // Given
  const document = fixture({ Character: { type: "string", minLength: 1, maxLength: 1 } });
  // When
  const { CharacterSchema } = await runtime(document);
  // Then
  assert.equal(CharacterSchema.safeParse("😀").success, true);
  assert.equal(CharacterSchema.safeParse("😀😀").success, false);
});

test("formats and bounds reject invalid wire values without date coercion", async () => {
  // Given
  const document = fixture({ Date: { type: "string", format: "date-time" },
    Text: { type: "string", minLength: 2, maxLength: 4, pattern: "^[A-Z]+$" },
    Number: { type: "number", exclusiveMinimum: 0, maximum: 5, multipleOf: 0.5 } });
  // When
  const { DateSchema, TextSchema, NumberSchema } = await runtime(document);
  // Then
  assert.equal(DateSchema.parse("2026-10-25T12:00:00Z"), "2026-10-25T12:00:00Z");
  assert.equal(DateSchema.safeParse("yesterday").success, false);
  assert.equal(TextSchema.safeParse("AB").success, true);
  assert.equal(TextSchema.safeParse("a").success, false);
  assert.equal(NumberSchema.safeParse(0.5).success, true);
  assert.equal(NumberSchema.safeParse(0).success, false);
  assert.equal(NumberSchema.safeParse(0.25).success, false);
});

for (const unsupported of [
  { type: "object", patternProperties: { x: { type: "string" } } },
  { type: "array", uniqueItems: true }, { not: { type: "string" } },
  { type: "string", format: "unrecognized" }, { $ref: "https://example.invalid/schema" },
]) {
  test(`unsupported semantics fail explicitly: ${JSON.stringify(unsupported)}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Unsupported: unsupported })), /unsupported|only local/i);
  });
}

test("unresolved refs and non-JSON recursion fail explicitly", () => {
  // Given / When / Then
  assert.throws(() => generate(fixture({ Item: { $ref: "#/components/schemas/Missing" } })), /outside registered closure/);
  assert.throws(() => generate(fixture({ Item: { type: "array", items: { $ref: "#/components/schemas/Item" } } })), /recursive/);
});

for (const malformed of [
  { type: "array", items: null }, { type: "object", properties: { value: null } },
  { type: "object", required: null }, { type: [] },
]) {
  test(`malformed schemas never become permissive defaults: ${JSON.stringify(malformed)}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Malformed: malformed })), /expected|nonempty/);
  });
}

test("invalid source names fail rather than silently sanitizing public types", () => {
  // Given / When / Then
  assert.throws(() => generate(fixture({ "Some-Name": {}, Some_Name: {} })), /invalid public symbol/);
});

test("generation is independent of component and property insertion order", () => {
  // Given
  const left = fixture({ Kind: { enum: ["x"] }, Item: { type: "object", properties: {
    z: { type: "string" }, a: { $ref: "#/components/schemas/Kind" },
  } } });
  const right = fixture({ Item: { properties: { a: { $ref: "#/components/schemas/Kind" }, z: { type: "string" } }, type: "object" }, Kind: { enum: ["x"] } });
  // When / Then
  assert.equal(generate(left), generate(right));
});

test("CLI check detects stale and missing files without writing", async () => {
  // Given
  const directory = await mkdtemp(join(tmpdir(), "maraclaw-contract-test-"));
  const input = join(directory, "input.json");
  const output = join(directory, "output.ts");
  await writeFile(input, JSON.stringify(fixture({ Item: { type: "string" } })));
  try {
    // When / Then
    await assert.rejects(main(["--input", input, "--output", output, "--check"]), /stale/);
    await assert.rejects(readFile(output), { code: "ENOENT" });
    await writeFile(output, "old\n");
    await assert.rejects(main(["--input", input, "--output", output, "--check"]), /stale/);
    assert.equal(await readFile(output, "utf8"), "old\n");
    await main(["--input", input, "--output", output]);
    await main(["--input", input, "--output", output, "--check"]);
  } finally {
    await rm(directory, { recursive: true });
  }
});

test("the checked real contract compiles and its WS schema validates actual event variants", async () => {
  // Given
  const document = JSON.parse(await readFile(new URL("../contracts/api-contract.json", import.meta.url), "utf8"));
  // When
  const { ChatEventType, ChatEventTypeValues, ChatEventTypeSchema, ChatServerEventSchema } = await runtime(document);
  // Then
  assert.deepEqual(Object.values(ChatEventType), ChatEventTypeValues);
  assert.equal(ChatEventTypeSchema.safeParse("text_progress").success, false);
  assert.equal(ChatServerEventSchema.safeParse({ type: ChatEventType.CONNECTED, session_id: "10000000-0000-4000-8000-000000000001" }).success, true);
  assert.equal(ChatServerEventSchema.safeParse({ type: ChatEventType.TURN_ACCEPTED, client_submission_id: "10000000-0000-4000-8000-000000000001", message_id: "10000000-0000-4000-8000-000000000002", session_id: "10000000-0000-4000-8000-000000000003" }).success, true);
  assert.equal(ChatServerEventSchema.safeParse({ type: "connected", session_id: "10000000-0000-4000-8000-000000000001" }).success, true);
  assert.equal(ChatServerEventSchema.safeParse({ type: "connected", session_id: "invalid" }).success, false);
  assert.equal(ChatServerEventSchema.safeParse({ type: "unknown_event" }).success, false);
  assert.equal(ChatServerEventSchema.safeParse({ type: "tool_call", arguments: { nested: [1, null, true] } }).success, true);
});
