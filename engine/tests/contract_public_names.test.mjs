import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, stripTypeScriptTypes } from "node:module";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { generate } from "../scripts/contract_codegen/generate.mjs";

const require = createRequire(new URL("../../web-e/package.json", import.meta.url));
const zodModule = process.env.CONTRACT_ZOD_MODULE ?? require.resolve("zod");

function fixture(schemas, names = {}) {
  return { components: { schemas }, "x-maraclaw-contract": {
    version: 1, roots: Object.keys(schemas), schemas: Object.keys(schemas), names,
  } };
}

async function runtime(source) {
  const javascript = stripTypeScriptTypes(source).replace('"zod"', JSON.stringify(pathToFileURL(zodModule).href));
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

test("explicit public names govern exports and referenced validators without changing validation", async () => {
  // Given two independent definitions and references whose names cannot be inferred by prefix stripping.
  const document = fixture({ "vendor__Wire-Input": { type: "string", minLength: 2 },
    _Wire: { type: "integer", minimum: 4 }, Envelope: { type: "object", required: ["input", "output"], properties: {
      input: { $ref: "#/components/schemas/vendor__Wire-Input" }, output: { $ref: "#/components/schemas/_Wire" },
    } } }, { "vendor__Wire-Input": "InputText", _Wire: "OutputCount" });
  // When
  const exports = await runtime(generate(document));
  // Then chosen names are usable at runtime and references retain the distinct validators.
  assert.deepEqual(Object.keys(exports).sort(), ["EnvelopeSchema", "InputTextSchema", "OutputCountSchema"]);
  assert.equal(exports.EnvelopeSchema.safeParse({ input: "ok", output: 4 }).success, true);
  assert.equal(exports.EnvelopeSchema.safeParse({ input: "x", output: 4 }).success, false);
  assert.equal(exports.EnvelopeSchema.safeParse({ input: "ok", output: 3 }).success, false);
  assert.equal(exports.EnvelopeSchema.safeParse({ input: "ok" }).success, false);
});

for (const name of ["", "9Name", "Some-Name", "Name.Output", "_Private", "app__Name", "JsonValue_Output", 42, null]) {
  test(`invalid public names fail before emission: ${JSON.stringify(name)}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Item: {} }, { Item: name })), /invalid public symbol/);
  });
}

for (const name of ["class", "default", "const", "enum", "await", "yield", "string", "number", "unknown", "never", "type", "infer", "interface"]) {
  test(`reserved public names fail before emission: ${name}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ Item: {} }, { Item: name })), /reserved public symbol/);
  });
}

for (const name of ["Domain", "DomainSchema", "DomainValues", "z"]) {
  test(`explicit names cannot collide with other generated exports: ${name}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ First: {}, Second: {} }, { First: "Domain", Second: name })), /export collision/);
  });
}

test("naming metadata for an unregistered schema fails instead of being silently ignored", () => {
  // Given / When / Then
  assert.throws(() => generate(fixture({ Item: {} }, { Missing: "Domain" })), /outside registered closure/);
});

for (const name of ["_Private", "app__module__Model", "JsonObject-Output"]) {
  test(`compiler artifacts require explicit engine ownership: ${name}`, () => {
    // Given / When / Then
    assert.throws(() => generate(fixture({ [name]: {} })), /invalid public symbol/);
  });
}

test("the real artifact exposes distinct domain contracts and recursive JSON validators", async () => {
  // Given
  const document = JSON.parse(await readFile(new URL("../contracts/api-contract.json", import.meta.url), "utf8"));
  const user = { id: "10000000-0000-4000-8000-000000000001", display_name: "Member", role: "member",
    is_active: true, created_at: "2026-10-25T12:00:00Z" };
  const summary = { id: user.id, role: "member", is_active: true, quota_message_limit: 10, quota_message_period: "daily",
    quota_messages_used: 1, quota_max_agents: 3, quota_agent_ttl_hours: 2 };
  // When
  const exports = await runtime(generate(document));
  // Then
  assert.equal(exports.AuthUserSchema.safeParse(user).success, true);
  assert.equal(exports.AuthUserSchema.safeParse(summary).success, false);
  assert.equal(exports.AdminUserSummarySchema.safeParse(summary).success, true);
  assert.equal(exports.AdminUserSummarySchema.safeParse(user).success, false);
  assert.equal(exports.TokenResponseSchema.safeParse({ access_token: "token", user }).success, true);
  assert.equal(exports.TokenResponseSchema.safeParse({ access_token: "token", user: summary }).success, false);
  assert.equal(exports.ControlActionResultSchema.safeParse({ status: "ok", screenshot: null }).success, true);
  assert.equal(exports.ControlActionResultSchema.safeParse({ status: "invalid" }).success, false);
  assert.equal(exports.ControlActionResultSchema.safeParse({ cookie_count: 1.5 }).success, false);
  assert.equal(exports.ApprovalDecisionRequestSchema.safeParse({ action: "approve" }).success, true);
  assert.equal(exports.ApprovalDecisionRequestSchema.safeParse({ action: "invalid" }).success, false);
  assert.equal(exports.ApprovalDecisionRequestSchema.safeParse({}).success, false);
  assert.deepEqual(exports.ApprovalAction, { APPROVE: "approve", REJECT: "reject" });
  assert.equal(exports.ApprovalActionSchema.safeParse("approve").success, true);
  for (const name of ["JsonValue", "JsonInputValue", "JsonOutputValue", "ToolCallValue"]) {
    assert.deepEqual(exports[`${name}Schema`].parse({ nested: [1, null, true, { value: "ok" }] }),
      { nested: [1, null, true, { value: "ok" }] });
    assert.equal(exports[`${name}Schema`].safeParse({ nested: undefined }).success, false);
  }
  for (const name of ["JsonInputObject", "JsonOutputObject"]) {
    assert.equal(exports[`${name}Schema`].safeParse({ nested: [1, null, true] }).success, true);
    assert.equal(exports[`${name}Schema`].safeParse([1, null, true]).success, false);
  }
});

for (const app of ["web-a", "web-e"]) {
  test(`${app} actual generated copy is current and every public symbol is free of compiler artifacts`, async () => {
    // Given
    const document = JSON.parse(await readFile(new URL("../contracts/api-contract.json", import.meta.url), "utf8"));
    // When
    const source = await readFile(new URL(`../../${app}/src/lib/engine-contracts.generated.ts`, import.meta.url), "utf8");
    // Then inspect every exported const/type, not only known problem examples.
    assert.equal(source, generate(document));
    const symbols = [...source.matchAll(/^export (?:type|const) (\S+) /gm)].map(match => match[1]);
    assert.ok(symbols.length > 400);
    assert.deepEqual(symbols.filter(name => !/^[A-Z][A-Za-z0-9]*$/.test(name)), []);
  });
}
